using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class SleepWellSetup
{
    private const string ProductName = "SleepWell";
    private const string PackageVersion = "0.4.7";
    private const string ResourceName = "SleepWellPayload.zip";
    private const long MaximumExpandedBytes = 500L * 1024L * 1024L;
    private const int MaximumFileCount = 10000;

    internal const int ExitSuccess = 0;
    internal const int ExitInvalidArguments = 2;
    internal const int ExitInvalidPackage = 3;
    internal const int ExitAlreadyInstalled = 4;
    internal const int ExitInstallFailed = 5;
    internal const int ExitUnsupportedPlatform = 6;

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length == 1 && String.Equals(args[0], "--verify-package", StringComparison.OrdinalIgnoreCase))
        {
            return VerifyPackageMode();
        }

        if (args.Length == 2 && String.Equals(args[0], "--test-install", StringComparison.OrdinalIgnoreCase))
        {
            return TestInstallMode(args[1]);
        }

        if (args.Length != 0)
        {
            Console.Error.WriteLine("사용법: SleepWellSetup.exe [--verify-package | --test-install <절대 경로>]");
            return ExitInvalidArguments;
        }

        if (!Environment.Is64BitOperatingSystem)
        {
            MessageBox.Show("SleepWell은 64비트 Windows 10/11에서 사용할 수 있습니다.", ProductName + " 설치", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return ExitUnsupportedPlatform;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        SetupForm form = new SetupForm();
        Application.Run(form);
        return form.ResultCode;
    }

    private static int VerifyPackageMode()
    {
        string temporaryRoot = Path.Combine(Path.GetTempPath(), "SleepWellInstallerVerify-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(temporaryRoot);
            PackageVerifier.ExtractAndVerify(temporaryRoot);
            Console.WriteLine("PACKAGE_OK version=" + PackageVersion);
            return ExitSuccess;
        }
        catch (PackageException ex)
        {
            Console.Error.WriteLine("PACKAGE_INVALID: " + ex.Message);
            return ExitInvalidPackage;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("VERIFY_FAILED: " + ex.Message);
            return ExitInstallFailed;
        }
        finally
        {
            TryDeleteOwnedTemporaryDirectory(temporaryRoot, "SleepWellInstallerVerify-");
        }
    }

    private static int TestInstallMode(string requestedTarget)
    {
        string target;
        try
        {
            target = Path.GetFullPath(requestedTarget);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("INVALID_TEST_TARGET: " + ex.Message);
            return ExitInvalidArguments;
        }

        if (!Path.IsPathRooted(requestedTarget) || !IsPermittedTestTarget(target))
        {
            Console.Error.WriteLine("INVALID_TEST_TARGET: 절대 경로이며 work 폴더 아래 test 폴더의 하위 경로여야 합니다.");
            return ExitInvalidArguments;
        }

        try
        {
            if (Directory.Exists(target) && Directory.GetFileSystemEntries(target).Length != 0)
            {
                Console.Error.WriteLine("ALREADY_INSTALLED: 대상 폴더가 비어 있지 않아 덮어쓰지 않습니다.");
                return ExitAlreadyInstalled;
            }

            if (File.Exists(target))
            {
                Console.Error.WriteLine("INVALID_TEST_TARGET: 대상 경로에 파일이 있습니다.");
                return ExitInvalidArguments;
            }

            if (Directory.Exists(target))
            {
                // The test contract permits an existing empty leaf. Removing only that
                // validated leaf lets the normal atomic Directory.Move path be tested.
                Directory.Delete(target, false);
            }

            InstallResult result = Installer.Install(target, false, false);
            Console.WriteLine(result.Message);
            return result.Code;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("TEST_INSTALL_FAILED: " + ex.Message);
            return ExitInstallFailed;
        }
    }

    private static bool IsPermittedTestTarget(string fullPath)
    {
        string root = Path.GetPathRoot(fullPath);
        string remainder = fullPath.Substring(root.Length);
        string[] parts = remainder.Split(new char[] { Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar }, StringSplitOptions.RemoveEmptyEntries);
        int workIndex = -1;
        int testIndex = -1;
        for (int i = 0; i < parts.Length; i++)
        {
            if (workIndex < 0 && String.Equals(parts[i], "work", StringComparison.OrdinalIgnoreCase))
            {
                workIndex = i;
            }
            else if (workIndex >= 0 && String.Equals(parts[i], "test", StringComparison.OrdinalIgnoreCase))
            {
                testIndex = i;
                break;
            }
        }
        return workIndex >= 0 && testIndex > workIndex && testIndex < parts.Length - 1;
    }

    private static void TryDeleteOwnedTemporaryDirectory(string path, string requiredPrefix)
    {
        try
        {
            string fullPath = Path.GetFullPath(path);
            string tempRoot = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string name = Path.GetFileName(fullPath);
            if (fullPath.StartsWith(tempRoot, StringComparison.OrdinalIgnoreCase) && name.StartsWith(requiredPrefix, StringComparison.Ordinal) && Directory.Exists(fullPath))
            {
                Directory.Delete(fullPath, true);
            }
        }
        catch
        {
            // Verification already completed. A locked temp directory is safer to leave behind.
        }
    }

    private sealed class SetupForm : Form
    {
        private readonly CheckBox desktopShortcut;
        private readonly CheckBox startMenuShortcut;
        private readonly Button installButton;
        private readonly Label status;

        internal int ResultCode { get; private set; }

        internal SetupForm()
        {
            ResultCode = ExitInvalidArguments;
            Text = "SleepWell 설치";
            ClientSize = new Size(540, 415);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = true;
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Malgun Gothic", 9F, FontStyle.Regular, GraphicsUnit.Point, 129);

            Label title = new Label();
            title.Text = "SleepWell";
            title.Font = new Font("Malgun Gothic", 22F, FontStyle.Bold, GraphicsUnit.Point, 129);
            title.Location = new Point(28, 24);
            title.AutoSize = true;
            Controls.Add(title);

            Label version = new Label();
            version.Text = "Windows용 프로토타입  " + PackageVersion;
            version.ForeColor = Color.DimGray;
            version.Location = new Point(32, 70);
            version.AutoSize = true;
            Controls.Add(version);

            Label description = new Label();
            description.Text = "다음날 수면 예측과 개인화 기록을 이 PC에서 실행합니다.\r\n" +
                               "Python을 별도로 설치할 필요가 없습니다.";
            description.Location = new Point(32, 105);
            description.Size = new Size(470, 48);
            Controls.Add(description);

            Label installPath = new Label();
            installPath.Text = "설치 위치\r\n" + Installer.DefaultInstallPath;
            installPath.Location = new Point(32, 160);
            installPath.Size = new Size(470, 48);
            installPath.ForeColor = Color.FromArgb(50, 50, 50);
            Controls.Add(installPath);

            desktopShortcut = new CheckBox();
            desktopShortcut.Text = "바탕 화면 바로가기 만들기";
            desktopShortcut.Checked = true;
            desktopShortcut.Location = new Point(35, 222);
            desktopShortcut.AutoSize = true;
            Controls.Add(desktopShortcut);

            startMenuShortcut = new CheckBox();
            startMenuShortcut.Text = "시작 메뉴 바로가기 만들기";
            startMenuShortcut.Checked = true;
            startMenuShortcut.Location = new Point(35, 250);
            startMenuShortcut.AutoSize = true;
            Controls.Add(startMenuShortcut);

            Label notice = new Label();
            notice.Text = "• 64비트 Windows 10/11용\r\n" +
                          "• 수면 기록은 %LOCALAPPDATA%\\SleepWell에 별도로 보존됩니다.\r\n" +
                          "• 20시 예측 저장에는 SleepWell이 트레이에서 실행 중이어야 합니다.\r\n" +
                          "• 현재 배포본은 디지털 서명되지 않은 연구용 프로토타입입니다.";
            notice.Location = new Point(32, 286);
            notice.Size = new Size(475, 75);
            notice.ForeColor = Color.DimGray;
            Controls.Add(notice);

            status = new Label();
            status.Text = " ";
            status.Location = new Point(32, 367);
            status.Size = new Size(355, 28);
            status.ForeColor = Color.FromArgb(32, 90, 140);
            Controls.Add(status);

            installButton = new Button();
            installButton.Text = "설치";
            installButton.Location = new Point(410, 363);
            installButton.Size = new Size(96, 34);
            installButton.Click += InstallButtonClick;
            Controls.Add(installButton);
        }

        private void InstallButtonClick(object sender, EventArgs e)
        {
            installButton.Enabled = false;
            desktopShortcut.Enabled = false;
            startMenuShortcut.Enabled = false;
            status.Text = "패키지를 검사하고 설치하는 중입니다…";
            status.Refresh();
            Cursor = Cursors.WaitCursor;

            InstallResult result = Installer.Install(Installer.DefaultInstallPath, desktopShortcut.Checked, startMenuShortcut.Checked);
            ResultCode = result.Code;
            Cursor = Cursors.Default;

            if (result.Code == ExitSuccess)
            {
                status.Text = "설치 완료";
                MessageBox.Show(result.Message, "SleepWell 설치 완료", MessageBoxButtons.OK, result.HasWarnings ? MessageBoxIcon.Warning : MessageBoxIcon.Information);
                Close();
                return;
            }

            status.Text = "설치하지 못했습니다.";
            MessageBox.Show(result.Message, "SleepWell 설치", MessageBoxButtons.OK, MessageBoxIcon.Error);
            installButton.Enabled = result.Code != ExitAlreadyInstalled;
            desktopShortcut.Enabled = installButton.Enabled;
            startMenuShortcut.Enabled = installButton.Enabled;
        }
    }

    private sealed class InstallResult
    {
        internal int Code;
        internal string Message;
        internal bool HasWarnings;
    }

    private static class Installer
    {
        internal static readonly string DefaultInstallPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", ProductName);

        internal static InstallResult Install(string targetPath, bool makeDesktopShortcut, bool makeStartMenuShortcut)
        {
            string target = Path.GetFullPath(targetPath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (Directory.Exists(target) || File.Exists(target))
            {
                return Failure(ExitAlreadyInstalled,
                    "SleepWell이 이미 설치되어 있습니다.\r\n\r\n" + target +
                    "\r\n\r\n실행 중인 SleepWell을 종료한 뒤, 기존 설치를 그대로 사용해 주세요. " +
                    "이 초기 설치기는 기존 파일을 자동으로 덮어쓰거나 삭제하지 않습니다.");
            }

            string parent = Path.GetDirectoryName(target);
            string stage = Path.Combine(parent, ".SleepWell-install-" + Guid.NewGuid().ToString("N"));
            bool activated = false;
            try
            {
                Directory.CreateDirectory(parent);
                Directory.CreateDirectory(stage);
                PackageVerifier.ExtractAndVerify(stage);

                if (Directory.Exists(target) || File.Exists(target))
                {
                    return Failure(ExitAlreadyInstalled, "설치 도중 다른 SleepWell 설치가 발견되어 중단했습니다. 기존 설치는 변경하지 않았습니다.");
                }

                Directory.Move(stage, target);
                activated = true;
            }
            catch (PackageException ex)
            {
                return Failure(ExitInvalidPackage, "설치 패키지 검증에 실패했습니다.\r\n\r\n" + ex.Message + "\r\n\r\n기존 설치와 사용자 기록은 변경하지 않았습니다.");
            }
            catch (Exception ex)
            {
                string stageNote = Directory.Exists(stage) ? "\r\n\r\n진단용 임시 폴더: " + stage : String.Empty;
                return Failure(ExitInstallFailed, "설치에 실패했습니다.\r\n\r\n" + ex.Message + stageNote + "\r\n\r\n사용자 기록은 변경하지 않았습니다.");
            }

            if (!activated)
            {
                return Failure(ExitInstallFailed, "설치 파일을 활성화하지 못했습니다.");
            }

            List<string> shortcutWarnings = new List<string>();
            string executable = Path.Combine(target, "SleepWell.exe");
            if (makeDesktopShortcut)
            {
                string desktopLink = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "SleepWell.lnk");
                TryCreateShortcut(desktopLink, executable, target, shortcutWarnings);
            }
            if (makeStartMenuShortcut)
            {
                string programs = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
                string startLink = Path.Combine(programs, "SleepWell.lnk");
                TryCreateShortcut(startLink, executable, target, shortcutWarnings);
            }

            string message = "SleepWell을 설치했습니다.\r\n\r\n" + target;
            if (shortcutWarnings.Count > 0)
            {
                message += "\r\n\r\n앱 설치는 완료됐지만 다음 바로가기를 만들지 못했습니다:\r\n- " + String.Join("\r\n- ", shortcutWarnings.ToArray());
            }
            return new InstallResult { Code = ExitSuccess, Message = message, HasWarnings = shortcutWarnings.Count > 0 };
        }

        private static InstallResult Failure(int code, string message)
        {
            return new InstallResult { Code = code, Message = message, HasWarnings = false };
        }

        private static void TryCreateShortcut(string shortcutPath, string executable, string workingDirectory, List<string> warnings)
        {
            try
            {
                if (File.Exists(shortcutPath))
                {
                    warnings.Add(shortcutPath + " (이미 존재함)");
                    return;
                }

                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType == null)
                {
                    throw new InvalidOperationException("Windows Script Host를 사용할 수 없습니다.");
                }
                object shell = Activator.CreateInstance(shellType);
                object shortcut = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { shortcutPath }, CultureInfo.InvariantCulture);
                Type shortcutType = shortcut.GetType();
                shortcutType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { executable }, CultureInfo.InvariantCulture);
                shortcutType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { workingDirectory }, CultureInfo.InvariantCulture);
                shortcutType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { executable + ",0" }, CultureInfo.InvariantCulture);
                shortcutType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "SleepWell 수면 예측" }, CultureInfo.InvariantCulture);
                shortcutType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null, CultureInfo.InvariantCulture);
            }
            catch (Exception ex)
            {
                warnings.Add(shortcutPath + " (" + ex.Message + ")");
            }
        }
    }

    private sealed class PackageException : Exception
    {
        internal PackageException(string message) : base(message) { }
    }

    private sealed class PackageManifest
    {
        public string version { get; set; }
        public List<ManifestFile> files { get; set; }
    }

    private sealed class ManifestFile
    {
        public string path { get; set; }
        public string sha256 { get; set; }
    }

    private sealed class PackageEntry
    {
        internal ZipArchiveEntry Entry;
        internal string RelativePath;
        internal bool IsDirectory;
    }

    private static class PackageVerifier
    {
        internal static void ExtractAndVerify(string destinationRoot)
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            using (Stream resource = assembly.GetManifestResourceStream(ResourceName))
            {
                if (resource == null)
                {
                    throw new PackageException("내장 패키지 리소스를 찾을 수 없습니다.");
                }
                using (ZipArchive archive = new ZipArchive(resource, ZipArchiveMode.Read, false))
                {
                    List<PackageEntry> entries = InspectArchive(archive);
                    ExtractEntries(entries, destinationRoot);
                }
            }
            VerifyManifest(destinationRoot);
        }

        private static List<PackageEntry> InspectArchive(ZipArchive archive)
        {
            List<PackageEntry> result = new List<PackageEntry>();
            HashSet<string> allPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            HashSet<string> filePaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            HashSet<string> directoryPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            long totalBytes = 0;
            int fileCount = 0;

            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                string normalized;
                bool isDirectory;
                NormalizeEntryPath(entry.FullName, out normalized, out isDirectory);
                RejectLink(entry);

                if (!allPaths.Add(normalized))
                {
                    throw new PackageException("중복된 ZIP 경로: " + normalized);
                }

                string[] components = normalized.Split('/');
                string ancestor = String.Empty;
                for (int i = 0; i < components.Length - 1; i++)
                {
                    ancestor = ancestor.Length == 0 ? components[i] : ancestor + "/" + components[i];
                    if (filePaths.Contains(ancestor))
                    {
                        throw new PackageException("파일과 폴더 경로가 충돌합니다: " + normalized);
                    }
                    directoryPaths.Add(ancestor);
                }

                if (isDirectory)
                {
                    if (filePaths.Contains(normalized))
                    {
                        throw new PackageException("파일과 폴더 경로가 충돌합니다: " + normalized);
                    }
                    directoryPaths.Add(normalized);
                }
                else
                {
                    if (directoryPaths.Contains(normalized))
                    {
                        throw new PackageException("파일과 폴더 경로가 충돌합니다: " + normalized);
                    }
                    filePaths.Add(normalized);
                    fileCount++;
                    if (fileCount > MaximumFileCount)
                    {
                        throw new PackageException("파일 수 제한을 초과했습니다.");
                    }
                    if (entry.Length < 0 || totalBytes > MaximumExpandedBytes - entry.Length)
                    {
                        throw new PackageException("압축 해제 크기 제한(500MB)을 초과했습니다.");
                    }
                    totalBytes += entry.Length;
                }

                result.Add(new PackageEntry { Entry = entry, RelativePath = normalized, IsDirectory = isDirectory });
            }

            if (!filePaths.Contains("package-manifest.json"))
            {
                throw new PackageException("package-manifest.json이 없습니다.");
            }
            return result;
        }

        private static void NormalizeEntryPath(string rawPath, out string normalized, out bool isDirectory)
        {
            if (String.IsNullOrEmpty(rawPath) || rawPath.IndexOf('\\') >= 0 || rawPath.StartsWith("/", StringComparison.Ordinal) || Path.IsPathRooted(rawPath))
            {
                throw new PackageException("허용되지 않는 ZIP 경로: " + rawPath);
            }

            isDirectory = rawPath.EndsWith("/", StringComparison.Ordinal);
            string candidate = isDirectory ? rawPath.Substring(0, rawPath.Length - 1) : rawPath;
            string[] parts = candidate.Split('/');
            if (parts.Length == 0)
            {
                throw new PackageException("빈 ZIP 경로가 있습니다.");
            }
            for (int i = 0; i < parts.Length; i++)
            {
                string part = parts[i];
                if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(" ", StringComparison.Ordinal) || part.EndsWith(".", StringComparison.Ordinal) || ContainsInvalidFileNameCharacter(part) || IsReservedWindowsName(part))
                {
                    throw new PackageException("허용되지 않는 ZIP 경로 구성요소: " + rawPath);
                }
            }
            normalized = String.Join("/", parts);
        }

        private static bool ContainsInvalidFileNameCharacter(string value)
        {
            char[] invalid = Path.GetInvalidFileNameChars();
            return value.IndexOfAny(invalid) >= 0 || value.IndexOf(':') >= 0;
        }

        private static bool IsReservedWindowsName(string value)
        {
            string stem = value;
            int dot = stem.IndexOf('.');
            if (dot >= 0)
            {
                stem = stem.Substring(0, dot);
            }
            string upper = stem.ToUpperInvariant();
            if (upper == "CON" || upper == "PRN" || upper == "AUX" || upper == "NUL")
            {
                return true;
            }
            if (upper.Length == 4 && (upper.StartsWith("COM", StringComparison.Ordinal) || upper.StartsWith("LPT", StringComparison.Ordinal)))
            {
                char suffix = upper[3];
                return suffix >= '1' && suffix <= '9';
            }
            return false;
        }

        private static void RejectLink(ZipArchiveEntry entry)
        {
            int unixMode = (entry.ExternalAttributes >> 16) & 0xF000;
            int dosAttributes = entry.ExternalAttributes & 0xFFFF;
            if (unixMode == 0xA000 || (dosAttributes & 0x0400) != 0)
            {
                throw new PackageException("링크 또는 재분석 지점은 허용되지 않습니다: " + entry.FullName);
            }
        }

        private static void ExtractEntries(List<PackageEntry> entries, string destinationRoot)
        {
            string root = Path.GetFullPath(destinationRoot).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string rootPrefix = root + Path.DirectorySeparatorChar;
            foreach (PackageEntry item in entries)
            {
                string relativeWindowsPath = item.RelativePath.Replace('/', Path.DirectorySeparatorChar);
                string outputPath = Path.GetFullPath(Path.Combine(root, relativeWindowsPath));
                if (!outputPath.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
                {
                    throw new PackageException("대상 폴더를 벗어나는 경로입니다: " + item.RelativePath);
                }

                if (item.IsDirectory)
                {
                    Directory.CreateDirectory(outputPath);
                    continue;
                }

                string parent = Path.GetDirectoryName(outputPath);
                Directory.CreateDirectory(parent);
                long written = 0;
                using (Stream input = item.Entry.Open())
                using (FileStream output = new FileStream(outputPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    byte[] buffer = new byte[81920];
                    int count;
                    while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        written += count;
                        if (written > item.Entry.Length || written > MaximumExpandedBytes)
                        {
                            throw new PackageException("파일의 실제 압축 해제 크기가 선언과 다릅니다: " + item.RelativePath);
                        }
                        output.Write(buffer, 0, count);
                    }
                }
                if (written != item.Entry.Length)
                {
                    throw new PackageException("파일의 실제 압축 해제 크기가 선언과 다릅니다: " + item.RelativePath);
                }
            }
        }

        private static void VerifyManifest(string root)
        {
            string manifestPath = Path.Combine(root, "package-manifest.json");
            PackageManifest manifest;
            try
            {
                string json = File.ReadAllText(manifestPath, new UTF8Encoding(false, true));
                manifest = new JavaScriptSerializer().Deserialize<PackageManifest>(json);
            }
            catch (Exception ex)
            {
                throw new PackageException("매니페스트를 읽을 수 없습니다: " + ex.Message);
            }

            if (manifest == null || !String.Equals(manifest.version, PackageVersion, StringComparison.Ordinal) || manifest.files == null)
            {
                throw new PackageException("매니페스트 버전 또는 files 항목이 올바르지 않습니다.");
            }

            Dictionary<string, string> expected = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            HashSet<string> expectedExact = new HashSet<string>(StringComparer.Ordinal);
            foreach (ManifestFile file in manifest.files)
            {
                string normalized;
                bool directory;
                if (file == null)
                {
                    throw new PackageException("매니페스트에 빈 파일 항목이 있습니다.");
                }
                NormalizeEntryPath(file.path, out normalized, out directory);
                if (directory || !String.Equals(normalized, file.path, StringComparison.Ordinal) || String.Equals(normalized, "package-manifest.json", StringComparison.OrdinalIgnoreCase))
                {
                    throw new PackageException("매니페스트 파일 경로가 올바르지 않습니다: " + file.path);
                }
                if (!IsSha256(file.sha256) || expected.ContainsKey(normalized))
                {
                    throw new PackageException("매니페스트 해시 또는 중복 경로가 올바르지 않습니다: " + normalized);
                }
                expected.Add(normalized, file.sha256);
                expectedExact.Add(normalized);
            }

            string[] extracted = Directory.GetFiles(root, "*", SearchOption.AllDirectories);
            int payloadFileCount = 0;
            foreach (string fullPath in extracted)
            {
                string relative = fullPath.Substring(root.TrimEnd(Path.DirectorySeparatorChar).Length + 1).Replace(Path.DirectorySeparatorChar, '/');
                if (String.Equals(relative, "package-manifest.json", StringComparison.OrdinalIgnoreCase))
                {
                    if (!String.Equals(relative, "package-manifest.json", StringComparison.Ordinal))
                    {
                        throw new PackageException("매니페스트 파일명의 대소문자가 올바르지 않습니다.");
                    }
                    continue;
                }

                payloadFileCount++;
                string expectedHash;
                if (!expected.TryGetValue(relative, out expectedHash))
                {
                    throw new PackageException("매니페스트에 없는 파일이 포함되어 있습니다: " + relative);
                }
                if (!expectedExact.Contains(relative))
                {
                    throw new PackageException("매니페스트와 ZIP 파일 경로의 대소문자가 다릅니다: " + relative);
                }
                string actualHash = ComputeSha256(fullPath);
                if (!String.Equals(actualHash, expectedHash, StringComparison.OrdinalIgnoreCase))
                {
                    throw new PackageException("SHA-256이 일치하지 않습니다: " + relative);
                }
            }

            if (payloadFileCount != expected.Count)
            {
                foreach (string expectedPath in expected.Keys)
                {
                    string fullPath = Path.Combine(root, expectedPath.Replace('/', Path.DirectorySeparatorChar));
                    if (!File.Exists(fullPath))
                    {
                        throw new PackageException("매니페스트에 기록된 파일이 없습니다: " + expectedPath);
                    }
                }
                throw new PackageException("매니페스트와 실제 파일 수가 일치하지 않습니다.");
            }

            if (!expectedExact.Contains("SleepWell.exe") || !ContainsPrefix(expectedExact, "runtime/") || !ContainsPrefix(expectedExact, "app/"))
            {
                throw new PackageException("필수 구성(SleepWell.exe, runtime/, app/)이 빠져 있습니다.");
            }
        }

        private static bool ContainsPrefix(IEnumerable<string> paths, string prefix)
        {
            foreach (string path in paths)
            {
                if (path.StartsWith(prefix, StringComparison.Ordinal))
                {
                    return true;
                }
            }
            return false;
        }

        private static bool IsSha256(string value)
        {
            if (String.IsNullOrEmpty(value) || value.Length != 64)
            {
                return false;
            }
            for (int i = 0; i < value.Length; i++)
            {
                char c = value[i];
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')))
                {
                    return false;
                }
            }
            return true;
        }

        private static string ComputeSha256(string path)
        {
            using (SHA256 algorithm = SHA256.Create())
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                byte[] hash = algorithm.ComputeHash(stream);
                StringBuilder result = new StringBuilder(hash.Length * 2);
                for (int i = 0; i < hash.Length; i++)
                {
                    result.Append(hash[i].ToString("x2", CultureInfo.InvariantCulture));
                }
                return result.ToString();
            }
        }
    }
}
