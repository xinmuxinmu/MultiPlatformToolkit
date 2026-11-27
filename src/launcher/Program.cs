using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Program
{
    private const string ConfiguredRoot = @"__PROJECT_ROOT__";
    private const string LauncherName = "\u542f\u52a8.bat";

    [STAThread]
    private static int Main()
    {
        string localRoot = AppDomain.CurrentDomain.BaseDirectory;
        string localLauncher = Path.Combine(localRoot, LauncherName);
        string configuredLauncher = Path.Combine(ConfiguredRoot, LauncherName);
        string root;
        string launcher;

        if (File.Exists(localLauncher))
        {
            root = localRoot;
            launcher = localLauncher;
        }
        else if (File.Exists(configuredLauncher))
        {
            root = ConfiguredRoot;
            launcher = configuredLauncher;
        }
        else
        {
            MessageBox.Show(
                "\u627e\u4e0d\u5230\u7a0b\u5e8f\u76ee\u5f55\u4e0b\u7684\u542f\u52a8.bat\u3002\n\n\u8bf7\u91cd\u65b0\u751f\u6210\u684c\u9762\u542f\u52a8\u5668\u3002",
                "MultiPlatformToolkit", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = launcher,
                WorkingDirectory = root,
                UseShellExecute = true,
                WindowStyle = ProcessWindowStyle.Normal
            });
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                "\u542f\u52a8\u811a\u672c\u65e0\u6cd5\u8fd0\u884c\uff1a\n" + ex.Message,
                "MultiPlatformToolkit", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
