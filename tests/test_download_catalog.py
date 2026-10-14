import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studio.tts import downloader


class QwenModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_root = downloader.PROJ_ROOT
        self.temp = tempfile.TemporaryDirectory()
        downloader.PROJ_ROOT = Path(self.temp.name)

    def tearDown(self):
        downloader.PROJ_ROOT = self.original_root
        self.temp.cleanup()

    def test_base_variants_are_distinct_voice_cloning_weight_catalog_entries(self):
        small = downloader.CATALOGO_MODELOS["qwen3_tts_06b_base"]
        large = downloader.CATALOGO_MODELOS["qwen3_tts_17b_base"]
        self.assertEqual(small["hf_repo"], "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
        self.assertEqual(large["hf_repo"], "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
        for model in (small, large):
            self.assertEqual(model["categoria"], "Voice Cloning")
            self.assertIn("clonagem", model["descricao"].lower())
            self.assertTrue(any(item["nome"] == "model.safetensors" for item in model["arquivos"]))
            self.assertTrue(any(item["nome"] == "speech_tokenizer/model.safetensors" for item in model["arquivos"]))
            self.assertTrue(all(item["url"].startswith(f"https://huggingface.co/{model['hf_repo']}/resolve/main/") for item in model["arquivos"]))
        self.assertNotEqual(small["subpasta"], large["subpasta"])
        self.assertIn("2.52", small["tamanho_estimado"])
        self.assertIn("4.54", large["tamanho_estimado"])

    def test_installation_status_uses_present_nonzero_local_repo_files(self):
        model = downloader.CATALOGO_MODELOS["qwen3_tts_06b_base"]
        folder = downloader.obter_pasta_modelo(model)
        self.assertFalse(downloader.verificar_modelo_instalado(model))
        for item in model["arquivos"]:
            path = folder / item["nome"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"local test fixture")
        self.assertTrue(downloader.verificar_modelo_instalado(model))
        (folder / model["arquivos"][0]["nome"]).write_bytes(b"")
        self.assertFalse(downloader.verificar_modelo_instalado(model))

    def test_model_destination_tracks_the_active_project_root(self):
        model = downloader.CATALOGO_MODELOS["qwen3_tts_17b_base"]
        self.assertEqual(
            downloader.obter_pasta_modelo(model),
            Path(self.temp.name) / "tools" / "models" / model["subpasta"],
        )

    def test_nested_tokenizer_files_install_without_network_access(self):
        model = downloader.CATALOGO_MODELOS["qwen3_tts_06b_base"]

        def write_fake_download(_url, target, on_progress=None, cancel_event=None):
            Path(target).write_bytes(b"small local fixture")
            if on_progress:
                on_progress(19, 19)
            return True

        with patch.object(downloader, "baixar_arquivo_com_progresso", side_effect=write_fake_download), \
             patch.object(downloader, "cfg_save"):
            result = downloader.baixar_modelo(model["id"], confirmar_callback=lambda _info: True)

        self.assertTrue(result["ok"], result["motivo"])
        self.assertTrue(downloader.verificar_modelo_instalado(model))
        self.assertTrue((downloader.obter_pasta_modelo(model) / "speech_tokenizer" / "model.safetensors").is_file())

    def test_complete_legacy_qwen_folders_are_reused(self):
        for model_id, legacy_name in (
            ("qwen3_tts_06b_base", "qwen3_tts_0_6b_base"),
            ("qwen3_tts_17b_base", "qwen3_tts_1_7b_base"),
        ):
            with self.subTest(model=model_id):
                model = downloader.CATALOGO_MODELOS[model_id]
                legacy = Path(self.temp.name) / "models" / legacy_name
                for item in model["arquivos"]:
                    target = legacy / item["nome"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"existing model asset")
                self.assertEqual(downloader.obter_pasta_modelo(model), legacy)
                self.assertTrue(downloader.verificar_modelo_instalado(model))

    def test_incomplete_legacy_folder_falls_back_to_default_destination(self):
        model = downloader.CATALOGO_MODELOS["qwen3_tts_17b_base"]
        legacy = Path(self.temp.name) / "models" / "qwen3_tts_1_7b_base"
        legacy.mkdir(parents=True)
        (legacy / "model.safetensors").write_bytes(b"partial")
        self.assertEqual(
            downloader.obter_pasta_modelo(model),
            Path(self.temp.name) / "tools" / "models" / model["subpasta"],
        )

    def test_qwen_models_are_first_in_available_model_list(self):
        models = downloader.obter_modelos_disponiveis()
        self.assertEqual([model.id for model in models[:2]], [
            "qwen3_tts_06b_base", "qwen3_tts_17b_base"
        ])

    def test_f5_base_catalog_includes_checkpoint_vocabulary_and_language_limit(self):
        model = downloader.CATALOGO_MODELOS["f5tts_base"]
        files = {item["nome"] for item in model["arquivos"]}
        self.assertIn("model_1200000.safetensors", files)
        self.assertIn("vocab.txt", files)
        self.assertIn("EN/ZH", model["nome"])
        self.assertIn("pt_BR", model["descricao"])


if __name__ == "__main__":
    unittest.main()
