#!/bin/bash
# Part Search Agent — launcher
#
# Streamlit demo (browser UI):
#   bash run.sh
#
# CLI demo (terminal, local JSON file):
#   bash run.sh cli
#   bash run.sh cli my_requirements.json --key sk-ant-...
#
# Istari agent (Anthropic):
#   bash run.sh istari --requirements-id <UUID> \
#       --provider anthropic --api-key sk-ant-... \
#       --istari-url https://your-instance.istari.app --istari-token <PAT>
#
# Istari agent (OpenAI):
#   bash run.sh istari --requirements-id <UUID> \
#       --provider openai --api-key sk-... \
#       --istari-url https://your-instance.istari.app --istari-token <PAT>
#
# Istari agent (AI Genesis Factory — LM hosted):
#   bash run.sh istari --requirements-id <UUID> \
#       --provider genesis --api-key <GENESIS_KEY> --model <model-name> \
#       --istari-url https://your-instance.istari.app --istari-token <PAT>
#
# Optional flags:
#   --model gpt-4o-mini   override the default model for the chosen provider
#   --dry-run             reason only, no uploads to Istari

set -e
cd "$(dirname "$0")"

# Create the virtual environment if it doesn't exist yet
if [ ! -d ".venv" ]; then
  echo "Setting up virtual environment (first run only)…"
  uv venv --python 3.12 .venv
  uv pip install -r requirements.txt
fi

MODE="${1:-ui}"
shift || true

case "$MODE" in
  ui)
    echo "Starting Part Search Agent (browser UI)…"
    .venv/bin/streamlit run demo.py
    ;;
  cli)
    .venv/bin/python demo_cli.py "$@"
    ;;
  istari)
    .venv/bin/python istari_part_search_agent.py "$@"
    ;;
  *)
    echo "Usage: bash run.sh [ui|cli|istari] [options]"
    echo ""
    echo "  ui                                   Browser UI (Streamlit)"
    echo "  cli [file] [--key KEY]               Terminal demo on a local JSON file"
    echo "  istari --requirements-id UUID        Full Istari pipeline"
    echo "         --provider anthropic|openai   LLM provider"
    echo "         --api-key KEY                 API key for the provider"
    echo "         --istari-url URL              Istari registry URL"
    echo "         --istari-token TOKEN          Istari Personal Access Token"
    echo "         [--model MODEL]               Override default model"
    echo "         [--dry-run]                   Skip uploads, reason only"
    exit 1
    ;;
esac
