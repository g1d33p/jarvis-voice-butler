"""Step 1i (2026-09-27): replies are read as replies."""

from test_whatsapp import FakeBrowser, _chat
from whatsapp import WhatsAppClient


def test_quoted_message_becomes_replying_to() -> None:
    parsed = WhatsAppClient._parse_messages(
        {
            "messages": [
                {
                    "meta": "[23:24, 26/9/2026] Jeevan: ",
                    "text": "Mm",
                    "quoted": "yeahhhhhhh",
                    "outgoing": True,
                }
            ]
        }
    )
    assert parsed == [
        {
            "sender": "Jeevan",
            "time": "23:24, 26/9/2026",
            "text": "Mm",
            "outgoing": True,
            "replying_to": "yeahhhhhhh",
        }
    ]


class _GrowingPane(FakeBrowser):
    """Messages appear a few at a time, like the real pane filling in."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.renders = 0

    async def evaluate(self, script):
        if "return waReadMessages(document" in script:
            self.renders += 1
            count = min(self.renders, 3)
            return {
                "messages": [
                    {
                        "meta": f"[10:0{i}, 27/9/2026] Ravi: ",
                        "text": f"m{i}",
                        "outgoing": False,
                    }
                    for i in range(count)
                ]
            }
        return await super().evaluate(script)


async def test_reading_waits_for_the_pane_to_fill() -> None:
    """Live: a read taken the moment the first bubble appeared returned 1 of many."""
    client = WhatsAppClient(browser=_GrowingPane(chats=[_chat("0413 chat")]))
    result = await client.read_messages("0413 chat", 10)
    assert [m["text"] for m in result["messages"]] == ["m0", "m1", "m2"]
