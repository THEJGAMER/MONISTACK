from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name):
    """Real command output captured live from the fleet's actual Dell
    S4048 (see ROADMAP.md 0.2) - frozen as text so a parser regression is
    caught without needing the switch. Re-capture via SSH if the parser's
    expected shape genuinely changes; don't hand-edit these files, since
    that defeats the point of testing against something real."""
    return (FIXTURES_DIR / name).read_text()
