using System;
using System.IO;
using UnityEditor;
using UnityEngine;
using TMPro;

public static class GameKOFontBundleBuilder
{
    public const string BundleName = "gameko_notosanscjkkr_sdf_u6000_3_23f1";
    public const string TestBundleName = "gameko_test_dialogue";
    public const string TextSchemaBundleName = "gameko_tmp_text_schema_u6000_3_23f1";
    private const string FontAsset = "Assets/Fonts/NotoSansCJKkr-Regular SDF.asset";
    private const string TestDialogueAsset = "Assets/Test/dialogue.json";
    private const string TextSchemaPrefab = "Assets/Test/gameko_tmp_text_schema.prefab";

    private static void EnsureTextSchemaPrefab()
    {
        var gameObject = new GameObject(
            "GameKO TMP UI Text Schema",
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(TextMeshProUGUI)
        );
        try
        {
            var text = gameObject.GetComponent<TextMeshProUGUI>();
            text.text = "前髪 後ろ髪 UI schema";
            text.font = AssetDatabase.LoadAssetAtPath<TMP_FontAsset>(FontAsset);
            PrefabUtility.SaveAsPrefabAsset(gameObject, TextSchemaPrefab);
            AssetDatabase.SaveAssets();
        }
        finally
        {
            UnityEngine.Object.DestroyImmediate(gameObject);
        }
    }

    public static void BuildFromCommandLine()
    {
        EnsureTextSchemaPrefab();
        var output = Path.GetFullPath(Path.Combine(Application.dataPath, "..", "AssetBundles"));
        Directory.CreateDirectory(output);
        var builds = new[]
        {
            new AssetBundleBuild
            {
                assetBundleName = BundleName,
                assetNames = new[] { FontAsset },
            },
            new AssetBundleBuild
            {
                assetBundleName = TestBundleName,
                assetNames = new[] { TestDialogueAsset },
            },
            new AssetBundleBuild
            {
                assetBundleName = TextSchemaBundleName,
                assetNames = new[] { TextSchemaPrefab },
            },
        };
        var manifest = BuildPipeline.BuildAssetBundles(
            output,
            builds,
            BuildAssetBundleOptions.ChunkBasedCompression | BuildAssetBundleOptions.ForceRebuildAssetBundle,
            BuildTarget.StandaloneWindows64
        );
        if (manifest == null || !File.Exists(Path.Combine(output, BundleName)))
            throw new InvalidOperationException("GameKO TMP font AssetBundle build failed.");
        if (!File.Exists(Path.Combine(output, TestBundleName)))
            throw new InvalidOperationException("GameKO dialogue test AssetBundle build failed.");
        if (!File.Exists(Path.Combine(output, TextSchemaBundleName)))
            throw new InvalidOperationException("GameKO TMP text schema AssetBundle build failed.");
        Debug.Log($"GameKO font AssetBundle: {Path.Combine(output, BundleName)}");
    }
}
