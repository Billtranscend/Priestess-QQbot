# Third-Party Notices

This repository contains material derived from, or distributed together with, the following
third-party works. Each remains under its own license.

Software that is installed or downloaded at deployment time and is not contained in this repository
(the Python packages from PyPI listed in `requirements.txt` and `requirements-official.txt`, such as
nonebot-adapter-qq, and the headless Chromium installed by Playwright) is not covered here; each
comes with its own license. The acknowledgements table in `README.md` lists the main ones.

## nonebot-plugin-skland

- Source: https://github.com/FrostN0v0/nonebot-plugin-skland
- Used in:
  - `plugins/skland_ef_theme/templates/` (`ef_gacha.html.jinja2`, `ef_gacha_macros.html.jinja2`):
    restyled versions of the templates of the same names shipped with nonebot-plugin-skland 0.7.1;
    the data fields and template logic come from the original work.
  - `plugins/arknights_garrison/__init__.py`: `_ask_garrison` is a copy of `SklandAPI.ark_garrison`
    from nonebot-plugin-skland 0.7.4, with one added check.
  - `plugins/skland_ef_theme/gacha.py` (`_ef_gacha_history`, `_pool_marks`, `_role_card`): adapted from
    `ef_gacha_history_handler` of nonebot-plugin-skland.
  - `plugins/skland_resource_resilience/__init__.py` (`fetch_file_list`): adapted from
    `GameResourceDownloader.fetch_file_list` of nonebot-plugin-skland.
- License: MIT

```
MIT License

Copyright (c) 2025 FrostN0v0

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Barlow Condensed

- Source: https://github.com/jpt/barlow
- Used in: `plugins/ef_theme/fonts/` (`BarlowCondensed-Medium.ttf`, `BarlowCondensed-SemiBold.ttf`,
  `BarlowCondensed-Bold.ttf`), unmodified.
- License: SIL Open Font License 1.1. The full license text is included as
  `plugins/ef_theme/fonts/OFL.txt`.

## Game data and assets

Operator, weapon, stage, enemy, activity and gacha pool data are downloaded at runtime from
AKEData (https://www.akedata.wiki/) and from Skland; they are not stored in this repository.
Only on the very first start of a fresh deployment, until the AKEData-based Endfield pool table has
been built, the pool table that nonebot-plugin-skland downloads from FrostN0v0/EndfieldGachaPoolTable
(https://github.com/FrostN0v0/EndfieldGachaPoolTable) is used instead.
Arknights activity and gacha pool times are downloaded at runtime from
yuanyan3060/ArknightsGameResource (https://github.com/yuanyan3060/ArknightsGameResource) and from
the public API of PRTS Wiki (https://prts.wiki/, content under CC BY-NC-SA 4.0); the rate-up
operators of the pools come from the pool detail file that PRTS Wiki keeps
(https://weedy.prts.wiki/gacha_table.json). None of this is stored in this repository either. Early gacha records are read from Heybox's gacha analysis pages
(https://www.xiaoheihe.cn/) only when a member asks for an import, and are stored in the bot's own
database, not in this repository.

Pictures used in the rendered images are fetched at runtime as well: game artwork and icons from
the sites that Skland, nonebot-plugin-skland and this project point to (Hypergryph's web CDN,
AKEData, PRTS Wiki and others); Endfield activity pictures from AKEData,
Endfield pool banners from the game's own pool page, and Arknights activity and pool banners from
PRTS Wiki. They are cached locally (`data/`, `cache/`) and never committed.
The screenshots under `docs/images/` contain names and artwork from Arknights and
Arknights: Endfield, which are the property of their respective rights holders and are shown only
to illustrate the bot's output.
