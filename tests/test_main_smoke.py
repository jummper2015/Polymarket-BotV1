"""Smoke test: bot.main imports + SecurityRuntime wired correctly."""
def test_main_module_imports_cleanly():
    import bot.main
    assert hasattr(bot.main, 'main')


def test_security_runtime_classes_importable():
    from bot.security_runtime import SecurityRuntime
    from bot.security import GuardConfig
    from bot.security_store import GuardRepository
    assert SecurityRuntime is not None
    assert GuardConfig is not None
    assert GuardRepository is not None


def test_main_wires_security_runtime():
    """Verify the main() function references SecurityRuntime."""
    import bot.main
    source = open(bot.main.__file__).read()
    assert "SecurityRuntime" in source
    assert "security_runtime" in source
    assert "GuardRepository" in source
    assert "GuardConfig" in source
