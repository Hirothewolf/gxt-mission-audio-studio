"""Extract original cutscene streams to project cache, never into the game."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.audio.cutscene import extract

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--game', required=True)
parser.add_argument('--project', required=True)
parser.add_argument('--saat', required=True)
args = parser.parse_args()
print('Exportando somente CUTSCENE original para cache isolado.', flush=True)
print(extract(args.game, args.project, args.saat), flush=True)
