from agent_lab.code_utils import describe_public_api, extract_python_code


def test_extracts_largest_fenced_block():
    text = "Sure!\n```python\nx = 1\n```\nand\n```python\ndef f():\n    return 2\n```"
    assert extract_python_code(text) == "def f():\n    return 2\n"


def test_falls_back_to_raw_text_without_fences():
    assert extract_python_code("  y = 3  ") == "y = 3\n"


def test_public_api_hides_implementation_and_private_functions():
    source = 'def add(a: int, b: int) -> int:\n    """Add."""\n    return a + b\n\ndef _helper():\n    pass\n'
    api = describe_public_api(source)
    assert "def add(a: int, b: int) -> int:" in api
    assert '"""Add."""' in api
    assert "return a + b" not in api
    assert "_helper" not in api


def test_public_api_reports_syntax_errors():
    assert "does not parse" in describe_public_api("def broken(:\n")
