import pytest

from app.core.config import REPO_ROOT, Settings
from app.knowledge.ingest import build_index

# tests must never depend on the developer's real .env (keys, fallback models, prices)
Settings.model_config["env_file"] = None

CORPUS_DIR = REPO_ROOT / "data" / "corpus"
CLAIMS_DB = REPO_ROOT / "data" / "claims.db"


@pytest.fixture(scope="session")
def index_path(tmp_path_factory) -> str:
    """Build the corpus index once per test session in a temp dir."""
    path = tmp_path_factory.mktemp("index") / "index.db"
    build_index(str(CORPUS_DIR), str(path), str(CLAIMS_DB), force=True, log=lambda *_: None)
    return str(path)
