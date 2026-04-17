from vaultcord.constants import MODE_ALL, MODE_LINKS, MODE_MEDIA, MODE_TEXT
from vaultcord.scraper import MessageScraper


class DummyClient:
    pass


def test_detect_mode_media() -> None:
    scraper = MessageScraper(client=DummyClient(), user_id="u1")
    assert scraper.detect_mode({"content": "hello", "attachments": [{"id": "a"}]}) == MODE_MEDIA


def test_detect_mode_links() -> None:
    scraper = MessageScraper(client=DummyClient(), user_id="u1")
    assert scraper.detect_mode({"content": "see https://example.com", "attachments": []}) == MODE_LINKS


def test_detect_mode_text() -> None:
    scraper = MessageScraper(client=DummyClient(), user_id="u1")
    assert scraper.detect_mode({"content": "plain text", "attachments": []}) == MODE_TEXT


def test_mode_matching() -> None:
    scraper = MessageScraper(client=DummyClient(), user_id="u1")
    assert scraper.mode_matches(MODE_TEXT, MODE_ALL)
    assert scraper.mode_matches(MODE_TEXT, MODE_TEXT)
    assert not scraper.mode_matches(MODE_LINKS, MODE_MEDIA)
