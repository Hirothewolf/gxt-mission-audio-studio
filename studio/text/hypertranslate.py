"""Inline multi-language translation for GXT text.

The translation pipeline is deliberately independent of Qt and Tk. Callers can
inject a translator for tests or provide a Google Translation v2 client for a
real run. GXT markup and line breaks are passed through untouched and each
plain-text span is translated separately, so the API never sees control codes.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


API_URL = "https://translation.googleapis.com/language/translate/v2"
USER_CONFIG_PATH = Path.home() / ".hypertranslate_config.json"
ALLOWED_ROUNDS = (1, 25, 50, 100)
PORTUGUESE_CODES = frozenset({"pt", "pt-BR", "pt-PT"})

# Language codes and labels are reference data from the supplied
# hypertranslate(3).py. No code from that Tkinter script is imported/executed.
LANGUAGES: dict[str, str] = {
    "af": "Africâner", "sq": "Albanês", "am": "Amárico", "ar": "Árabe",
    "hy": "Armênio", "az": "Azerbaijano", "eu": "Basco", "be": "Bielorrusso",
    "bn": "Bengali", "bs": "Bósnio", "bg": "Búlgaro", "ca": "Catalão",
    "ceb": "Cebuano", "ny": "Chichewa", "zh-CN": "Chinês (simplificado)",
    "zh-TW": "Chinês (tradicional)", "co": "Corso", "hr": "Croata", "cs": "Tcheco",
    "da": "Dinamarquês", "nl": "Holandês", "en": "Inglês", "eo": "Esperanto",
    "et": "Estoniano", "tl": "Filipino", "fi": "Finlandês", "fr": "Francês",
    "fy": "Frísio", "gl": "Galego", "ka": "Georgiano", "de": "Alemão",
    "el": "Grego", "gu": "Gujarati", "ht": "Crioulo haitiano", "ha": "Hauçá",
    "haw": "Havaiano", "iw": "Hebraico", "hi": "Hindi", "hmn": "Hmong",
    "hu": "Húngaro", "is": "Islandês", "ig": "Igbo", "id": "Indonésio",
    "ga": "Irlandês", "it": "Italiano", "ja": "Japonês", "jw": "Javanês",
    "kn": "Canarês", "kk": "Cazaque", "km": "Khmer", "rw": "Kinyarwanda",
    "ko": "Coreano", "ku": "Curdo", "ky": "Quirguiz", "lo": "Laosiano",
    "la": "Latim", "lv": "Letão", "lt": "Lituano", "lb": "Luxemburguês",
    "mk": "Macedônio", "mg": "Malgaxe", "ms": "Malaio", "ml": "Malaiala",
    "mt": "Maltês", "mi": "Maori", "mr": "Marathi", "mn": "Mongol",
    "my": "Birmanês", "ne": "Nepalês", "no": "Norueguês", "or": "Odia",
    "ps": "Pachto", "fa": "Persa", "pl": "Polonês", "ro": "Romeno", "ru": "Russo",
    "sm": "Samoano", "gd": "Gaélico escocês", "sr": "Sérvio", "st": "Sesoto",
    "sn": "Shona", "sd": "Sindi", "si": "Cingalês", "sk": "Eslovaco",
    "sl": "Esloveno", "so": "Somali", "es": "Espanhol", "su": "Sundanês",
    "sw": "Suaíli", "sv": "Sueco", "tg": "Tadjique", "ta": "Tâmil", "tt": "Tártaro",
    "te": "Telugu", "th": "Tailandês", "tr": "Turco", "tk": "Turcomeno",
    "uk": "Ucraniano", "ur": "Urdu", "ug": "Uigur", "uz": "Uzbeque",
    "vi": "Vietnamita", "cy": "Galês", "xh": "Xhosa", "yi": "Iídiche",
    "yo": "Iorubá", "zu": "Zulu", "pt": "Português",
}

# Keep all tilde-delimited tokens, including unknown GTA controls, and actual
# line breaks out of requests. This covers ~z~, ~k~~ACTION~, ~1~, and ~n~.
_PROTECTED_RE = re.compile(r"(~[^~\r\n]{1,80}~|\r\n|\r|\n)")
_LEADING_WS_RE = re.compile(r"^\s*", re.DOTALL)
_TRAILING_WS_RE = re.compile(r"\s*$", re.DOTALL)


class HypertranslateError(RuntimeError):
    """An actionable translation configuration or API error."""


class HypertranslateCancelled(HypertranslateError):
    """Raised after a cancellation request has been observed."""


@dataclass(frozen=True)
class TranslationStage:
    index: int
    kind: str
    source_language: str | None
    target_language: str
    text: str
    characters_sent: int


@dataclass(frozen=True)
class HypertranslateResult:
    text: str
    source_text: str
    identity: object
    route_seed: int
    route: tuple[str, ...]
    final_language: str
    stages: tuple[TranslationStage, ...]
    stages_path: str | None = None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "source_text": self.source_text,
            "identity": copy.deepcopy(self.identity),
            "route_seed": self.route_seed,
            "route": list(self.route),
            "final_language": self.final_language,
            "stages": [asdict(stage) for stage in self.stages],
            "stages_path": self.stages_path,
        }


def load_user_credentials(path: str | Path | None = None) -> dict[str, str]:
    """Read legacy credentials without exposing them to project preferences."""
    source = Path(path) if path is not None else USER_CONFIG_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "auth_mode": str(data.get("auth_mode") or ""),
        "api_key": str(data.get("api_key") or os.environ.get("GOOGLE_API_KEY", "")),
        "service_account_path": str(
            data.get("json_path") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        ),
    }


def _safe_api_message(message: str, api_key: str = "") -> str:
    message = str(message or "").replace(API_URL, "Google Translation v2")
    if api_key:
        message = message.replace(api_key, "[credencial omitida]")
    message = re.sub(r"https?://\S+", "endereço da API", message)
    return message[:500]


class GoogleTranslationV2:
    """Small standard-library client for Google Cloud Translation Basic v2."""

    def __init__(self, *, api_key: str = "", service_account_path: str = "", timeout: float = 30):
        self.api_key = api_key.strip()
        self.service_account_path = service_account_path.strip()
        self.timeout = timeout
        self._credentials = None
        self._auth_request = None
        if bool(self.api_key) == bool(self.service_account_path):
            raise HypertranslateError("Escolha uma chave de API ou um JSON de conta de serviço nas configurações.")
        if self.service_account_path:
            path = Path(self.service_account_path).expanduser()
            if not path.is_file():
                raise HypertranslateError("O arquivo JSON da conta de serviço não foi encontrado.")
            try:
                from google.auth.transport.requests import Request
                from google.oauth2 import service_account
            except ImportError as exc:
                raise HypertranslateError(
                    "Para autenticação por conta de serviço, este ambiente precisa ter google-auth e requests."
                ) from exc
            try:
                self._credentials = service_account.Credentials.from_service_account_file(
                    str(path), scopes=["https://www.googleapis.com/auth/cloud-translation"]
                )
            except Exception as exc:
                raise HypertranslateError("O JSON da conta de serviço não pôde ser carregado.") from exc
            self._auth_request = Request()

    def translate(self, text: str, target: str, source: str | None = None,
                  cancel_event: threading.Event | None = None) -> tuple[str, str | None]:
        if cancel_event and cancel_event.is_set():
            raise HypertranslateCancelled("Tradução cancelada.")
        body = {"q": text, "target": target, "format": "text"}
        if source:
            body["source"] = source
        url = API_URL
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._credentials is not None:
            try:
                if not self._credentials.valid:
                    self._credentials.refresh(self._auth_request)
                headers["Authorization"] = "Bearer " + self._credentials.token
            except Exception as exc:
                raise HypertranslateError("Não foi possível autenticar a conta de serviço.") from exc
        else:
            url += "?" + urllib.parse.urlencode({"key": self.api_key})
        request = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
            except (ValueError, UnicodeError, AttributeError):
                detail = ""
            if exc.code in (401, 403):
                lead = "A autenticação foi recusada. Confira a chave/conta e se a Cloud Translation API está ativa."
            elif exc.code == 429:
                lead = "O limite de uso da Cloud Translation API foi atingido."
            else:
                lead = f"A Cloud Translation API respondeu com erro {exc.code}."
            safe = _safe_api_message(detail, self.api_key)
            raise HypertranslateError(lead + (" " + safe if safe else "")) from None
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", ""))
            raise HypertranslateError("Não foi possível conectar à Cloud Translation API." +
                                      ((" " + _safe_api_message(reason, self.api_key)) if reason else "")) from None
        except (TimeoutError, OSError):
            raise HypertranslateError("A conexão com a Cloud Translation API expirou ou foi interrompida.") from None
        except (ValueError, KeyError, TypeError):
            raise HypertranslateError("A resposta da Cloud Translation API veio em formato inesperado.") from None
        try:
            item = payload["data"]["translations"][0]
            translated = item["translatedText"]
            if not isinstance(translated, str):
                raise TypeError
            detected = item.get("detectedSourceLanguage")
            return translated, str(detected) if detected else source
        except (KeyError, IndexError, TypeError):
            raise HypertranslateError("A resposta da Cloud Translation API não contém uma tradução válida.") from None


def choose_route(rounds: int, final_language: str, seed: int | None = None) -> tuple[int, tuple[str, ...]]:
    if rounds not in ALLOWED_ROUNDS:
        raise ValueError("A quantidade de etapas precisa ser 1, 25, 50 ou 100.")
    if final_language not in LANGUAGES:
        raise ValueError("Escolha um idioma final compatível com a lista do tradutor.")
    if seed is None:
        seed = secrets.randbits(64)
    pool = [code for code in LANGUAGES if code not in PORTUGUESE_CODES and code != final_language]
    if rounds > len(pool):
        raise ValueError(f"Há apenas {len(pool)} idiomas intermediários únicos disponíveis.")
    return seed, tuple(random.Random(seed).sample(pool, rounds))


def _split_plain_spans(text: str):
    return _PROTECTED_RE.split(text)


def _translate_preserving_controls(text: str, target: str, source: str | None,
                                   translator, cancel_event: threading.Event) -> tuple[str, str | None, int]:
    result: list[str] = []
    detected_source = source
    sent = 0
    for part in _split_plain_spans(text):
        if not part:
            continue
        if _PROTECTED_RE.fullmatch(part):
            result.append(part)
            continue
        leading = _LEADING_WS_RE.match(part).group(0)
        trailing = _TRAILING_WS_RE.search(part).group(0)
        end = len(part) - len(trailing) if trailing else len(part)
        core = part[len(leading):end]
        if not core:
            result.append(part)
            continue
        if cancel_event.is_set():
            raise HypertranslateCancelled("Tradução cancelada.")
        translated, detected = translator.translate(core, target, detected_source, cancel_event)
        if not isinstance(translated, str):
            raise HypertranslateError("O tradutor retornou um texto inválido.")
        result.extend((leading, translated, trailing))
        detected_source = detected_source or detected
        sent += len(core)
    return "".join(result), detected_source, sent


def _write_checkpoint(path: Path | None, data: dict) -> str | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return str(path)


def run_hypertranslate(
    source_text: str,
    *,
    rounds: int,
    final_language: str,
    translator,
    identity=None,
    seed: int | None = None,
    cancel_event: threading.Event | None = None,
    checkpoint_path: str | Path | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> HypertranslateResult:
    """Run an injectable route and checkpoint the source and every completed stage."""
    if not isinstance(source_text, str) or not source_text.strip():
        raise ValueError("Selecione uma linha com texto antes de traduzir.")
    route_seed, route = choose_route(rounds, final_language, seed)
    cancel_event = cancel_event or threading.Event()
    path = Path(checkpoint_path) if checkpoint_path is not None else None
    now = datetime.now(timezone.utc).isoformat()
    snapshot_identity = copy.deepcopy(identity)
    record = {
        "format": "gxt-hypertranslate-run-v1", "job_id": uuid.uuid4().hex,
        "created_at": now, "updated_at": now, "status": "running",
        "source_text": source_text, "identity": snapshot_identity,
        "route_seed": route_seed, "route": list(route), "final_language": final_language,
        "stages": [{"index": 0, "kind": "original", "source_language": None,
                    "target_language": None, "text": source_text, "characters_sent": 0}],
        "error": None,
    }
    stage_results: list[TranslationStage] = []
    current = source_text
    current_language = None
    total = rounds + 1
    _write_checkpoint(path, record)
    try:
        for index, target in enumerate((*route, final_language), 1):
            if cancel_event.is_set():
                raise HypertranslateCancelled("Tradução cancelada.")
            kind = "intermediate" if index <= rounds else "final"
            before_language = current_language
            current, detected, sent = _translate_preserving_controls(
                current, target, current_language, translator, cancel_event
            )
            stage = TranslationStage(index, kind, before_language, target, current, sent)
            stage_results.append(stage)
            current_language = target
            record["stages"].append(asdict(stage))
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_checkpoint(path, record)
            if progress:
                progress(index, total, target)
        record["status"] = "complete"
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        saved_path = _write_checkpoint(path, record)
        return HypertranslateResult(
            text=current, source_text=source_text, identity=snapshot_identity,
            route_seed=route_seed, route=route, final_language=final_language,
            stages=tuple(stage_results), stages_path=saved_path,
        )
    except HypertranslateCancelled:
        record["status"] = "cancelled"
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_checkpoint(path, record)
        raise
    except Exception as exc:
        record["status"] = "error"
        record["error"] = str(exc)[:500]
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_checkpoint(path, record)
        raise

