from types import SimpleNamespace

from scripts import smoke_feishu_cardkit_stream as smoke


class RecordingClient:
    def __init__(self, _app_id, _app_secret):
        self.operations = []

    def create_card_instance(self, card):
        self.operations.append(("create", card))
        return "card-1"

    def send_card_by_reference(self, chat_id, card_id):
        self.operations.append(("send", chat_id, card_id))
        return "message-1"

    def update_card_element_content(
        self, card_id, element_id, content, sequence
    ):
        self.operations.append(
            ("update", card_id, element_id, content, sequence)
        )

    def finish_streaming_card(self, card_id, sequence):
        self.operations.append(("finish", card_id, sequence))


def test_cardkit_smoke_runs_create_send_two_updates_and_finish(monkeypatch, capsys):
    holder = {}

    def client_factory(app_id, app_secret):
        holder["client"] = RecordingClient(app_id, app_secret)
        return holder["client"]

    monkeypatch.setattr(
        smoke,
        "_arguments",
        lambda: SimpleNamespace(chat_id="oc-smoke"),
    )
    monkeypatch.setattr(
        smoke.Settings,
        "from_env",
        lambda **_kwargs: SimpleNamespace(
            feishu_bot_app_id="app",
            feishu_bot_app_secret="secret",
        ),
    )
    monkeypatch.setattr(smoke, "FeishuClient", client_factory)

    assert smoke.main() == 0

    operations = holder["client"].operations
    assert [item[0] for item in operations] == [
        "create", "send", "update", "update", "finish"
    ]
    assert operations[2][-2:] == ("CardKit smoke", 1)
    assert operations[3][-2:] == ("CardKit smoke PASS", 2)
    assert operations[4] == ("finish", "card-1", 3)
    assert "PASS stage=finalize provider_code=0" in capsys.readouterr().out


def test_cardkit_smoke_requires_explicit_chat_id(monkeypatch, capsys):
    monkeypatch.setattr(
        smoke,
        "_arguments",
        lambda: SimpleNamespace(chat_id=""),
    )

    assert smoke.main() == 2
    assert "FAIL stage=config" in capsys.readouterr().out
