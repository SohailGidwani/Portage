"""Secret-redaction unit tests (Phase 7). Pure functions — no DB, no Docker."""

from portage_agent.agent.nodes.common import non_python_context, non_python_sources
from portage_agent.agent.nodes.redaction import is_denied_path, scrub


def test_deny_list_paths():
    for p in (".env", ".env.production", "config/credentials.json", "certs/server.pem",
              "id_rsa", ".ssh/id_ed25519.pub", ".npmrc"):
        assert is_denied_path(p), p
    for p in ("src/app.py", "tests/conftest.py", "templates/index.html", "README.md",
              "environment.py"):
        assert not is_denied_path(p), p


def test_scrub_known_token_shapes():
    text = (
        "aws = 'AKIAIOSFODNN7EXAMPLE'\n"
        "gh = 'ghp_" + "a" * 36 + "'\n"
        "openai = 'sk-" + "b" * 24 + "'\n"
        "slack = 'xoxb-1234567890-abcdefghij'\n"
    )
    out = scrub(text)
    assert "AKIA" not in out and "[REDACTED:aws-access-key]" in out
    assert "ghp_" not in out and "[REDACTED:github-token]" in out
    assert "sk-" not in out and "[REDACTED:openai-key]" in out
    assert "xoxb-" not in out and "[REDACTED:slack-token]" in out


def test_scrub_private_key_block():
    text = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA…\n"
            "-----END RSA PRIVATE KEY-----\n")
    out = scrub(text)
    assert "MIIEow" not in out and "[REDACTED:private-key]" in out


def test_scrub_generic_assignment_but_not_placeholders():
    out = scrub('PASSWORD = "hunter2hunter2"\nsecret_key: "prodSecretValue99"\n')
    assert "hunter2" not in out and out.count("[REDACTED:secret-assignment]") == 2
    # obvious placeholders survive (redacting them would just add noise)
    keep = scrub('password = "changeme-example"\napi_key = "<your-key-here>"\n')
    assert "changeme-example" in keep and "<your-key-here>" in keep


def test_scrub_url_credentials():
    out = scrub("db = 'postgresql://portage:supersecretpw@db:5432/portage'")
    assert "supersecretpw" not in out and "[REDACTED:url-credentials]" in out
    # username survives; only the password segment is replaced
    assert "portage:" in out


def test_scrub_leaves_normal_code_alone():
    code = (
        "def create_item(name):\n"
        "    token = tokenize(name)  # not a secret\n"
        "    return {'id': 1, 'name': name}\n"
    )
    assert scrub(code) == code


def test_non_python_context_surveys_semantic_text_and_omits_secrets(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "page.html").write_text("{{ request.form['name'] }}")
    (tmp_path / "data.sql").write_text("INSERT INTO user VALUES ('hash');")
    (tmp_path / ".env").write_text("PASSWORD=should-never-enter-prompts")
    (tmp_path / "logo.png").write_bytes(b"not text context")

    context = non_python_context(str(tmp_path))

    assert "request.form" in context
    assert "INSERT INTO user" in context
    assert "should-never-enter-prompts" not in context
    assert "logo.png" not in context
    assert non_python_sources(str(tmp_path))["logo.png"] == ""
