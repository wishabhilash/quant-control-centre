"""Code/project tests incl. safe ZIP import (spec 19)."""
import io
import zipfile
import pytest

from app.models.strategy import StrategyDocument, CodeBlock, Schedule
from app.services.code_service import CodeService, CodeError
from app.storage.strategy_store import StrategyStore


def _seed(store):
    doc = StrategyDocument(strategy_id="STR-001", name="x",
                          code=CodeBlock(entrypoint="main.py", files={"main.py": "print(1)"}))
    store.create_document(doc)


def test_encode_decode_files(store, settings):
    _seed(store)
    svc = CodeService(store, settings)
    code = svc.encode_project({"a/b.py": "x=1", "main.py": "y=2"}, "main.py")
    assert set(code.files) == {"a/b.py", "main.py"}
    decoded = svc.decode_project(code)
    assert decoded["a/b.py"] == "x=1"


def test_reject_path_traversal(store, settings):
    svc = CodeService(store, settings)
    with pytest.raises(CodeError):
        svc.encode_project({"../evil.py": "x"}, "main.py")


def test_reject_reserved_path(store, settings):
    svc = CodeService(store, settings)
    with pytest.raises(CodeError):
        svc.encode_project({"strategy.log": "x", "main.py": "y"}, "main.py")


def test_zip_import_happy(store, settings):
    _seed(store)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", "print('from zip')")
        zf.writestr("lib/util.py", "X=1")
    svc = CodeService(store, settings)
    code, imported = svc.import_zip("STR-001", buf.getvalue())
    assert "lib/util.py" in code.files
    assert code.entrypoint == "main.py"


def test_zip_rejects_symlink(store, settings):
    _seed(store)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)  # symlink
        zf.writestr(info, "x")
        zf.writestr("main.py", "print(1)")
    svc = CodeService(store, settings)
    with pytest.raises(CodeError):
        svc.import_zip("STR-001", buf.getvalue())


def test_zip_rejects_traversal(store, settings):
    _seed(store)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.py", "x=1")
        zf.writestr("main.py", "print(1)")
    svc = CodeService(store, settings)
    with pytest.raises(CodeError):
        svc.import_zip("STR-001", buf.getvalue())
