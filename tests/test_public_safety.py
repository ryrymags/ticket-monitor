from scripts.check_public_safety import forbidden_path_rule, scan_text


def rules(text: str) -> set[str]:
    return {finding.rule for finding in scan_text("example.txt", text)}


def test_forbids_private_runtime_paths():
    assert forbidden_path_rule("config.yaml") == "private configuration"
    assert forbidden_path_rule("secrets/profile/Cookies") == "browser or authentication data"
    assert forbidden_path_rule("ticket_history 6.json") == "ticket history"
    assert forbidden_path_rule("uptime_log 7.json") == "uptime history"


def test_allows_public_examples_and_source_files():
    assert forbidden_path_rule("config.example.yaml") is None
    assert forbidden_path_rule("tests/fixtures/config_maximal.yaml") is None
    assert rules("event_id: EXAMPLEEVENT0001") == set()
    assert rules("https://discord.com/api/webhooks/example/not-a-token") == set()


def test_detects_personal_identity_and_home_paths():
    private_email = "person" + "@" + "icloud" + ".com"
    private_path = "/" + "Users" + "/alice/project/file.py"
    assert "personal Apple email" in rules(private_email)
    assert "absolute macOS home path" in rules(private_path)


def test_detects_live_credential_and_event_shapes():
    webhook = "https://discord.com/api/webhooks/" + "1" * 18 + "/" + "x" * 40
    event_id = "event_id: " + "A1" * 8
    assert "live-looking Discord webhook" in rules(webhook)
    assert "production-looking Ticketmaster event ID" in rules(event_id)
