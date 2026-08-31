# Third-party components

GameKO itself is MIT licensed. It downloads the following tools only when the matching engine needs them.

- [XUnity.AutoTranslator](https://github.com/bbepis/XUnity.AutoTranslator) — MIT, Unity runtime translation.
- [BepInEx](https://github.com/BepInEx/BepInEx) — LGPL-2.1, Unity IL2CPP plugin loader.
- [UndertaleModTool](https://github.com/UnderminersTeam/UndertaleModTool) — GPL-3.0, GameMaker data read/write CLI. It is invoked as a separate program.
- [Argos Translate](https://github.com/argosopentech/argos-translate) — MIT/CC0, optional offline translation.
- [UnityPy](https://github.com/K0lb3/UnityPy) — MIT, experimental Unity TextAsset/AssetBundle extraction and verified repacking. GameKO pins the supported minor series to 1.25.x.
- [Unity Catalog Unlocker](https://github.com/KKING-ar/Unity-Catalog-Unlocker) and [AddressablesTools](https://github.com/nesrak1/AddressablesTools) — MIT, reference implementations for disabling Addressables catalog CRC checks after an intentional local AssetBundle edit. GameKO uses its own validated catalog.bin/catalog.json patcher and regenerates `catalog.hash`.
- [Noto Sans CJK KR Regular](https://github.com/notofonts/noto-cjk) — SIL Open Font License 1.1. GameKO bundles the user-provided `NotoSansCJKkr-Regular.otf`; the complete font license is included as `NotoSansKR-OFL.txt`.
- The Unity 6000.3.23f1 TMP fallback bundle contains the user-provided `NotoSansCJKkr-Regular SDF.asset`, generated from the same OTF, and is distributed under the same SIL Open Font License 1.1.

Each downloaded component remains governed by its own license. Game assets and text remain the copyright of their respective owners.
