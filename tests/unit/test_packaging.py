"""The version is declared in three places; they must agree.

setup.py and package.xml both need a literal (ament_python requires the
package.xml one), so instead of build-time machinery this test pins them to
``fair_ros.__version__``.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import fair_ros

REPO = Path(__file__).resolve().parents[2]


def test_version_declarations_agree():
    package_xml = ET.parse(REPO / "package.xml").getroot()
    assert package_xml.findtext("version") == fair_ros.__version__

    setup_py = (REPO / "setup.py").read_text()
    match = re.search(r"version=\"([^\"]+)\"", setup_py)
    assert match is not None, "setup.py has no version= literal"
    assert match.group(1) == fair_ros.__version__


def test_changelog_covers_current_version():
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert fair_ros.__version__ in changelog
