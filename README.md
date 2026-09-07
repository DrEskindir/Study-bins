# Study Bins

If your large Anki collection makes the deck browser feel overwhelming, Study Bins gives you a clear place to start. Group the decks you want into simple, named bins, focus on one area at a time, and optionally see only the cards that are due for that area.

Spend less time deciding what to review next and more time actually practicing. Study Bins keeps your existing decks and scheduling intact while turning a crowded collection into a focused study plan.

## Installation

**Via AnkiWeb (recommended):**
1. Tools → Add-ons → Get Add-ons...
2. Enter the add-on code: `  `
3. Restart Anki.



### Install from file

This method does not provide automatic updates.

1. Download `study_bins.ankiaddon` from the GitHub Releases page.
2. In Anki, open **Tools > Add-ons > Install from file**.
3. Select the downloaded file.
4. Restart Anki.

Check the GitHub Releases page manually for updates when using this method.

## How to use it

Open **Tools > Study Bins > Manage Bins** to create a bin and choose the decks it contains.

- Select a bin to focus the deck browser on its decks.
- Select **Due Cards Only** to study due cards from the focused bin.
- Select **Show All Decks** to return to the full deck tree.

Due cards stay separated by deck. New cards are not included, and the original decks and their settings are not changed.

## Shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+Shift+B | Open the bin manager |
| Ctrl+Shift+F | Turn focus on or off |
| Ctrl+Shift+Right / Ctrl+Shift+Left | Move between bins |
| Ctrl+Shift+D | Turn due-card mode on or off |

The focus and due-card shortcuts work in the deck browser.

## Updating

Add-ons installed through AnkiWeb are checked for updates by Anki. You can also check manually in **Tools > Add-ons**.

Add-ons installed from a downloaded `.ankiaddon` file are not updated automatically. Download the latest release and install it again when a new version is available.

## Troubleshooting

If the add-on stops working after an Anki update, restart Anki and install the latest release. If the problem continues, report it with your Anki version and a description of what happened.

If a due-card session appears empty, confirm that the selected decks contain cards that are currently due.

## Support

Please open an issue on GitHub for problems or feature requests. Include your Anki version and the steps that caused the problem.

## Packaging a release

Run these commands from the add-on directory. The archive must contain the files directly, without a wrapping folder.

```text
cd study_bins_repo
find . -name "__pycache__" -o -name "*.pyc" -exec rm -rf {} +
zip -r ../study_bins.ankiaddon __init__.py manifest.json config.json
```

The `.ankiaddon` file should contain only `__init__.py`, `manifest.json`, and `config.json`.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
