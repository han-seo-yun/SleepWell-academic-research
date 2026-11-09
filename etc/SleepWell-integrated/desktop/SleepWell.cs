using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("SleepWell")]
[assembly: System.Reflection.AssemblyProduct("SleepWell")]
[assembly: System.Reflection.AssemblyDescription("SleepWell Windows desktop launcher")]
[assembly: System.Reflection.AssemblyCompany("SleepWell")]
[assembly: System.Reflection.AssemblyVersion("0.4.7.0")]
[assembly: System.Reflection.AssemblyFileVersion("0.4.7.0")]

namespace SleepWellDesktop
{
    internal sealed class ReadyMetadata
    {
        public string App;
        public string Url;
        public int Pid;
        public string InstanceId;
    }

    internal sealed class CommandLineOptions
    {
        public bool SelfTest;
        public bool SmokeTest;
        public bool ResearchLab;
        public string DataDirectory;

        public static CommandLineOptions Parse(string[] args)
        {
            CommandLineOptions result = new CommandLineOptions();
            for (int i = 0; i < args.Length; i++)
            {
                string value = args[i];
                if (String.Equals(value, "--self-test", StringComparison.OrdinalIgnoreCase))
                {
                    result.SelfTest = true;
                }
                else if (String.Equals(value, "--smoke-test", StringComparison.OrdinalIgnoreCase))
                {
                    result.SmokeTest = true;
                }
                else if (String.Equals(value, "--research-lab", StringComparison.OrdinalIgnoreCase))
                {
                    result.ResearchLab = true;
                }
                else if (String.Equals(value, "--data-dir", StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    result.DataDirectory = args[++i];
                }
            }
            return result;
        }
    }

    internal static class LauncherContract
    {
        public const string ProductName = "SleepWell";
        public const string Version = "0.4.7";
        public const int StartupTimeoutMilliseconds = 30000;

        public static string NormalizeDataDirectory(string value)
        {
            string full = Path.GetFullPath(value).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            return full.Length == 0 ? Path.GetPathRoot(Path.GetFullPath(value)) : full;
        }

        public static string GetMutexName(string dataDirectory)
        {
            string normalized = NormalizeDataDirectory(dataDirectory).ToLowerInvariant();
            byte[] digest;
            using (SHA256 algorithm = SHA256.Create())
            {
                digest = algorithm.ComputeHash(Encoding.UTF8.GetBytes(normalized));
            }
            StringBuilder prefix = new StringBuilder(16);
            for (int i = 0; i < 8; i++)
            {
                prefix.Append(digest[i].ToString("x2"));
            }
            return @"Local\SleepWell.Desktop." + prefix.ToString();
        }

        public static string QuoteArgument(string value)
        {
            if (value == null)
            {
                value = String.Empty;
            }
            if (value.Length > 0 && value.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            {
                return value;
            }

            StringBuilder result = new StringBuilder();
            result.Append('"');
            int backslashes = 0;
            for (int i = 0; i < value.Length; i++)
            {
                char current = value[i];
                if (current == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (current == '"')
                {
                    result.Append('\\', backslashes * 2 + 1);
                    result.Append('"');
                    backslashes = 0;
                    continue;
                }
                result.Append('\\', backslashes);
                backslashes = 0;
                result.Append(current);
            }
            result.Append('\\', backslashes * 2);
            result.Append('"');
            return result.ToString();
        }

        public static string BuildHostArguments(string scriptPath, string dataDirectory, string readyFile)
        {
            string[] values = new string[]
            {
                "-B", "-X", "utf8", scriptPath, "--data-dir", dataDirectory, "--ready-file", readyFile
            };
            StringBuilder result = new StringBuilder();
            for (int i = 0; i < values.Length; i++)
            {
                if (i > 0)
                {
                    result.Append(' ');
                }
                result.Append(QuoteArgument(values[i]));
            }
            return result.ToString();
        }

        public static bool IsSafeLoopbackUrl(string value, out Uri uri)
        {
            uri = null;
            Uri parsed;
            if (!Uri.TryCreate(value, UriKind.Absolute, out parsed))
            {
                return false;
            }
            if (!String.Equals(parsed.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase) ||
                !(String.Equals(parsed.Host, "127.0.0.1", StringComparison.OrdinalIgnoreCase) ||
                  String.Equals(parsed.Host, "localhost", StringComparison.OrdinalIgnoreCase)) ||
                parsed.Port <= 0 || !String.IsNullOrEmpty(parsed.UserInfo) ||
                !String.Equals(parsed.AbsolutePath, "/", StringComparison.Ordinal) ||
                !String.IsNullOrEmpty(parsed.Query) || !String.IsNullOrEmpty(parsed.Fragment))
            {
                return false;
            }
            uri = parsed;
            return true;
        }

        public static ReadyMetadata ReadReadyFile(string path)
        {
            string json = File.ReadAllText(path, new UTF8Encoding(false));
            Dictionary<string, object> data = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
            if (data == null)
            {
                throw new InvalidDataException("준비 정보가 비어 있습니다.");
            }

            ReadyMetadata result = new ReadyMetadata();
            result.App = RequireString(data, "app");
            result.Url = RequireString(data, "url");
            result.InstanceId = RequireString(data, "instance_id");
            object rawPid;
            if (!data.TryGetValue("pid", out rawPid))
            {
                throw new InvalidDataException("준비 정보에 pid가 없습니다.");
            }
            result.Pid = Convert.ToInt32(rawPid);
            return result;
        }

        private static string RequireString(Dictionary<string, object> data, string key)
        {
            object value;
            if (!data.TryGetValue(key, out value) || value == null || String.IsNullOrWhiteSpace(Convert.ToString(value)))
            {
                throw new InvalidDataException("준비 정보에 " + key + "가 없습니다.");
            }
            return Convert.ToString(value);
        }

        public static bool ValidateReadyMetadata(ReadyMetadata metadata, int expectedPid, out Uri appUri)
        {
            appUri = null;
            return metadata != null &&
                   String.Equals(metadata.App, ProductName, StringComparison.Ordinal) &&
                   metadata.Pid == expectedPid && expectedPid > 0 &&
                   !String.IsNullOrWhiteSpace(metadata.InstanceId) &&
                   IsSafeLoopbackUrl(metadata.Url, out appUri);
        }

        public static bool VerifyHealth(ReadyMetadata metadata, int expectedPid)
        {
            Uri baseUri;
            if (!ValidateReadyMetadata(metadata, expectedPid, out baseUri))
            {
                return false;
            }
            Uri healthUri = new Uri(baseUri, "/api/desktop-health");
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(healthUri);
            request.Method = "GET";
            request.Timeout = 2000;
            request.ReadWriteTimeout = 2000;
            request.AllowAutoRedirect = false;
            request.Proxy = null;
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            {
                if (response.StatusCode != HttpStatusCode.OK)
                {
                    return false;
                }
                using (Stream stream = response.GetResponseStream())
                using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                {
                    Dictionary<string, object> health = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
                    if (health == null)
                    {
                        return false;
                    }
                    string app = Convert.ToString(health.ContainsKey("app") ? health["app"] : null);
                    string instanceId = Convert.ToString(health.ContainsKey("instance_id") ? health["instance_id"] : null);
                    int pid = health.ContainsKey("pid") ? Convert.ToInt32(health["pid"]) : 0;
                    return String.Equals(app, ProductName, StringComparison.Ordinal) &&
                           String.Equals(instanceId, metadata.InstanceId, StringComparison.Ordinal) &&
                           pid == expectedPid;
                }
            }
        }

        public static bool TryReadAndVerify(string readyFile, int expectedPid, out ReadyMetadata metadata)
        {
            metadata = null;
            try
            {
                ReadyMetadata candidate = ReadReadyFile(readyFile);
                Uri ignored;
                if (!ValidateReadyMetadata(candidate, expectedPid, out ignored) || !VerifyHealth(candidate, expectedPid))
                {
                    return false;
                }
                metadata = candidate;
                return true;
            }
            catch
            {
                return false;
            }
        }

        public static string FindEdge()
        {
            string[] candidates = new string[]
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Microsoft", "Edge", "Application", "msedge.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Microsoft", "Edge", "Application", "msedge.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Microsoft", "Edge", "Application", "msedge.exe")
            };
            for (int i = 0; i < candidates.Length; i++)
            {
                if (File.Exists(candidates[i]))
                {
                    return candidates[i];
                }
            }
            return null;
        }

        public static string BuildApplicationUrl(string baseUrl, bool researchLab)
        {
            Uri uri;
            if (!IsSafeLoopbackUrl(baseUrl, out uri))
            {
                throw new InvalidOperationException("안전한 로컬 주소가 아닙니다.");
            }
            return researchLab ? new Uri(uri, "research-lab").AbsoluteUri : uri.AbsoluteUri;
        }

        public static void OpenApplicationUrl(string baseUrl, bool researchLab)
        {
            Uri uri = new Uri(BuildApplicationUrl(baseUrl, researchLab), UriKind.Absolute);
            string edge = FindEdge();
            if (!String.IsNullOrEmpty(edge))
            {
                try
                {
                    ProcessStartInfo edgeInfo = new ProcessStartInfo(edge, "--app=" + QuoteArgument(uri.AbsoluteUri));
                    edgeInfo.UseShellExecute = false;
                    Process edgeProcess = Process.Start(edgeInfo);
                    if (edgeProcess != null)
                    {
                        edgeProcess.Dispose();
                        return;
                    }
                }
                catch
                {
                    // Fall through to the user's default browser.
                }
            }
            ProcessStartInfo browserInfo = new ProcessStartInfo(uri.AbsoluteUri);
            browserInfo.UseShellExecute = true;
            Process browserProcess = Process.Start(browserInfo);
            if (browserProcess != null) browserProcess.Dispose();
        }

        public static int RunSelfTest()
        {
            try
            {
                Uri uri;
                if (!IsSafeLoopbackUrl("http://127.0.0.1:54321/", out uri)) return 11;
                if (!IsSafeLoopbackUrl("http://localhost:54321/", out uri)) return 12;
                if (IsSafeLoopbackUrl("https://127.0.0.1:54321/", out uri)) return 13;
                if (IsSafeLoopbackUrl("http://example.com:54321/", out uri)) return 14;
                if (IsSafeLoopbackUrl("http://[::1]:54321/", out uri)) return 15;
                if (IsSafeLoopbackUrl("http://127.0.0.1:54321/path", out uri)) return 16;
                if (IsSafeLoopbackUrl("http://127.0.0.1:54321/?next=x", out uri)) return 17;
                if (IsSafeLoopbackUrl("http://user@127.0.0.1:54321/", out uri)) return 18;
                if (QuoteArgument("plain") != "plain") return 19;
                if (QuoteArgument("C:\\한글 경로\\file.py") != "\"C:\\한글 경로\\file.py\"") return 20;
                if (QuoteArgument("a\"b") != "\"a\\\"b\"") return 21;
                if (!GetMutexName("C:\\Temp\\Sleep Well").StartsWith(@"Local\SleepWell.Desktop.", StringComparison.Ordinal)) return 22;
                if (BuildApplicationUrl("http://127.0.0.1:54321/", false) != "http://127.0.0.1:54321/") return 23;
                if (BuildApplicationUrl("http://127.0.0.1:54321/", true) != "http://127.0.0.1:54321/research-lab") return 24;
                try
                {
                    BuildApplicationUrl("http://127.0.0.1:54321/other", true);
                    return 25;
                }
                catch (InvalidOperationException) { }
                CommandLineOptions parsed = CommandLineOptions.Parse(new string[] { "--research-lab", "--data-dir", "C:\\Sleep Well" });
                if (!parsed.ResearchLab || parsed.DataDirectory != "C:\\Sleep Well") return 26;
                return 0;
            }
            catch
            {
                return 99;
            }
        }
    }

    internal sealed class HostController : IDisposable
    {
        private readonly string packageRoot;
        private readonly string dataDirectory;
        private readonly string readyFile;
        private Process child;
        private int ownedChildPid;
        private ReadyMetadata ready;
        private bool shutdownRequested;

        public event EventHandler UnexpectedExit;

        public HostController(string packageRootValue, string dataDirectoryValue)
        {
            packageRoot = packageRootValue;
            dataDirectory = LauncherContract.NormalizeDataDirectory(dataDirectoryValue);
            readyFile = Path.Combine(dataDirectory, "desktop-ready.json");
        }

        public string DataDirectory { get { return dataDirectory; } }
        public ReadyMetadata Ready { get { return ready; } }
        public bool IsRunning { get { return child != null && !child.HasExited && ready != null; } }

        public async Task StartAsync()
        {
            Directory.CreateDirectory(dataDirectory);
            string python = Path.Combine(packageRoot, "runtime", "python.exe");
            string script = Path.Combine(packageRoot, "app", "desktop_host.py");
            if (!File.Exists(python) || !File.Exists(script))
            {
                throw new FileNotFoundException("배포 패키지의 실행 파일이 누락되었습니다.");
            }

            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = python;
            info.Arguments = LauncherContract.BuildHostArguments(script, dataDirectory, readyFile);
            info.WorkingDirectory = packageRoot;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardInput = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;

            child = new Process();
            child.StartInfo = info;
            child.EnableRaisingEvents = true;
            child.OutputDataReceived += delegate { };
            child.ErrorDataReceived += delegate { };
            child.Exited += ChildExited;
            if (!child.Start())
            {
                throw new InvalidOperationException("SleepWell 서비스를 시작하지 못했습니다.");
            }
            ownedChildPid = child.Id;
            try
            {
                child.BeginOutputReadLine();
                child.BeginErrorReadLine();

                Stopwatch timer = Stopwatch.StartNew();
                while (timer.ElapsedMilliseconds < LauncherContract.StartupTimeoutMilliseconds)
                {
                    if (child.HasExited)
                    {
                        throw new InvalidOperationException("SleepWell 서비스가 시작 중 종료되었습니다.");
                    }
                    ReadyMetadata candidate;
                    if (LauncherContract.TryReadAndVerify(readyFile, child.Id, out candidate))
                    {
                        ready = candidate;
                        return;
                    }
                    await Task.Delay(150).ConfigureAwait(false);
                }
                throw new TimeoutException("SleepWell 서비스 시작 시간이 초과되었습니다.");
            }
            catch
            {
                Shutdown();
                throw;
            }
        }

        public bool VerifyCurrentReady(out ReadyMetadata metadata)
        {
            metadata = null;
            if (child == null || child.HasExited)
            {
                return false;
            }
            if (!LauncherContract.TryReadAndVerify(readyFile, child.Id, out metadata))
            {
                return false;
            }
            ready = metadata;
            return true;
        }

        private void ChildExited(object sender, EventArgs args)
        {
            ready = null;
            if (!shutdownRequested)
            {
                EventHandler handler = UnexpectedExit;
                if (handler != null)
                {
                    handler(this, EventArgs.Empty);
                }
            }
        }

        public bool Shutdown()
        {
            shutdownRequested = true;
            Process owned = child;
            if (owned == null)
            {
                return true;
            }

            try
            {
                if (owned.HasExited)
                {
                    return true;
                }
                try
                {
                    owned.StandardInput.WriteLine("shutdown");
                    owned.StandardInput.Flush();
                }
                catch
                {
                    // A broken control pipe proceeds to the owned-process fallback.
                }

                if (owned.WaitForExit(8000))
                {
                    return true;
                }

                // The fallback targets only the exact Process object this launcher spawned.
                // Never locate or terminate a process using ready-file metadata.
                if (Object.ReferenceEquals(owned, child) && owned.Id == ownedChildPid && !owned.HasExited)
                {
                    owned.Kill();
                }
                bool stopped = owned.WaitForExit(3000) || owned.HasExited;
                if (!stopped) shutdownRequested = false;
                return stopped;
            }
            catch
            {
                try
                {
                    bool stopped = owned.HasExited;
                    if (!stopped) shutdownRequested = false;
                    return stopped;
                }
                catch
                {
                    shutdownRequested = false;
                    return false;
                }
            }
            finally
            {
                ready = null;
            }
        }

        public void Dispose()
        {
            Shutdown();
            if (child != null)
            {
                child.Dispose();
                child = null;
            }
        }
    }

    internal sealed class MainWindow : Form
    {
        private readonly HostController controller;
        private readonly Button openButton;
        private readonly Button folderButton;
        private readonly Button exitButton;
        private readonly Label statusLabel;
        private readonly NotifyIcon trayIcon;
        private readonly bool openResearchLabInitially;
        private bool explicitExit;

        public MainWindow(HostController controllerValue, bool researchLabInitially)
        {
            controller = controllerValue;
            openResearchLabInitially = researchLabInitially;
            controller.UnexpectedExit += ControllerUnexpectedExit;

            Text = "SleepWell";
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(470, 245);
            MinimumSize = new Size(486, 284);
            Font = new Font("Malgun Gothic", 9F);
            Icon = SystemIcons.Information;

            Label title = new Label();
            title.Text = "SleepWell";
            title.Font = new Font("Malgun Gothic", 18F, FontStyle.Bold);
            title.AutoSize = true;
            title.Location = new Point(26, 22);
            Controls.Add(title);

            Label version = new Label();
            version.Text = "Windows 앱 0.4.7";
            version.ForeColor = Color.DimGray;
            version.AutoSize = true;
            version.Location = new Point(31, 62);
            Controls.Add(version);

            statusLabel = new Label();
            statusLabel.Text = "수면 예측 서비스를 시작하고 있습니다…";
            statusLabel.AutoEllipsis = true;
            statusLabel.Location = new Point(30, 93);
            statusLabel.Size = new Size(410, 24);
            Controls.Add(statusLabel);

            openButton = new Button();
            openButton.Text = "SleepWell 열기";
            openButton.Location = new Point(30, 127);
            openButton.Size = new Size(125, 38);
            openButton.Enabled = false;
            openButton.Click += async delegate { await OpenCurrentAsync(); };
            Controls.Add(openButton);

            folderButton = new Button();
            folderButton.Text = "데이터 폴더 열기";
            folderButton.Location = new Point(172, 127);
            folderButton.Size = new Size(125, 38);
            folderButton.Click += delegate { OpenDataDirectory(); };
            Controls.Add(folderButton);

            exitButton = new Button();
            exitButton.Text = "종료";
            exitButton.Location = new Point(314, 127);
            exitButton.Size = new Size(112, 38);
            exitButton.Click += async delegate { await ExitApplicationAsync(); };
            Controls.Add(exitButton);

            Label note = new Label();
            note.Text = "창을 닫아도 알림 영역에서 계속 실행됩니다. 매일 20시 예측 저장을 위해 PC가 켜져 있고 SleepWell이 실행 중이어야 합니다.";
            note.ForeColor = Color.FromArgb(70, 70, 70);
            note.Location = new Point(30, 184);
            note.Size = new Size(405, 45);
            Controls.Add(note);

            ContextMenuStrip menu = new ContextMenuStrip();
            ToolStripMenuItem menuOpen = new ToolStripMenuItem("SleepWell 열기");
            menuOpen.Click += async delegate { await OpenCurrentAsync(); };
            ToolStripMenuItem menuFolder = new ToolStripMenuItem("데이터 폴더 열기");
            menuFolder.Click += delegate { OpenDataDirectory(); };
            ToolStripMenuItem menuExit = new ToolStripMenuItem("종료");
            menuExit.Click += async delegate { await ExitApplicationAsync(); };
            menu.Items.Add(menuOpen);
            menu.Items.Add(menuFolder);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(menuExit);

            trayIcon = new NotifyIcon();
            trayIcon.Text = "SleepWell - 예측 저장 실행 중";
            trayIcon.Icon = SystemIcons.Information;
            trayIcon.ContextMenuStrip = menu;
            trayIcon.Visible = true;
            trayIcon.DoubleClick += async delegate { await OpenCurrentAsync(); };

            Shown += async delegate { await StartServiceAsync(); };
            FormClosing += MainWindowClosing;
        }

        private async Task StartServiceAsync()
        {
            try
            {
                await controller.StartAsync();
                if (IsDisposed) return;
                statusLabel.Text = "실행 중 · 데이터는 이 PC에 별도로 저장됩니다.";
                openButton.Enabled = true;
                await OpenCurrentAsync(openResearchLabInitially);
            }
            catch (Exception exception)
            {
                if (IsDisposed) return;
                statusLabel.Text = "시작 실패: " + exception.Message;
                openButton.Enabled = false;
                trayIcon.Text = "SleepWell - 시작 실패";
            }
        }

        private async Task OpenCurrentAsync(bool researchLab = false)
        {
            openButton.Enabled = false;
            try
            {
                ReadyMetadata metadata = await Task.Run(delegate
                {
                    ReadyMetadata verified;
                    return controller.VerifyCurrentReady(out verified) ? verified : null;
                });
                if (metadata == null)
                {
                    statusLabel.Text = "서비스 연결을 확인할 수 없습니다.";
                    return;
                }
                LauncherContract.OpenApplicationUrl(metadata.Url, researchLab);
                statusLabel.Text = researchLab
                    ? "실행 중 · 연구 분석실을 열었습니다."
                    : "실행 중 · SleepWell 화면을 열었습니다.";
            }
            catch (Exception exception)
            {
                statusLabel.Text = "화면을 열지 못했습니다: " + exception.Message;
            }
            finally
            {
                openButton.Enabled = controller.IsRunning;
            }
        }

        private void OpenDataDirectory()
        {
            try
            {
                Directory.CreateDirectory(controller.DataDirectory);
                ProcessStartInfo info = new ProcessStartInfo("explorer.exe", LauncherContract.QuoteArgument(controller.DataDirectory));
                info.UseShellExecute = false;
                Process.Start(info);
            }
            catch (Exception exception)
            {
                statusLabel.Text = "데이터 폴더를 열지 못했습니다: " + exception.Message;
            }
        }

        private void ControllerUnexpectedExit(object sender, EventArgs args)
        {
            if (IsDisposed) return;
            BeginInvoke((MethodInvoker)delegate
            {
                statusLabel.Text = "SleepWell 서비스가 예상치 못하게 종료되었습니다.";
                openButton.Enabled = false;
                trayIcon.Text = "SleepWell - 서비스 종료됨";
            });
        }

        private void MainWindowClosing(object sender, FormClosingEventArgs args)
        {
            if (explicitExit || args.CloseReason == CloseReason.WindowsShutDown || args.CloseReason == CloseReason.TaskManagerClosing)
            {
                explicitExit = true;
                trayIcon.Visible = false;
                controller.Shutdown();
                return;
            }
            args.Cancel = true;
            Hide();
            trayIcon.ShowBalloonTip(2500, "SleepWell", "20시 예측 저장을 위해 알림 영역에서 계속 실행됩니다. 종료하려면 알림 영역 메뉴에서 ‘종료’를 선택하세요.", ToolTipIcon.Info);
        }

        private async Task ExitApplicationAsync()
        {
            if (explicitExit) return;
            explicitExit = true;
            statusLabel.Text = "SleepWell을 종료하고 있습니다…";
            openButton.Enabled = false;
            folderButton.Enabled = false;
            exitButton.Enabled = false;
            bool stopped = await Task.Run(delegate { return controller.Shutdown(); });
            if (!stopped)
            {
                explicitExit = false;
                statusLabel.Text = "서비스를 종료하지 못했습니다. 잠시 후 다시 시도해 주세요.";
                folderButton.Enabled = true;
                exitButton.Enabled = true;
                trayIcon.Visible = true;
                return;
            }
            trayIcon.Visible = false;
            Close();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                trayIcon.Dispose();
                controller.Dispose();
            }
            base.Dispose(disposing);
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            CommandLineOptions options = CommandLineOptions.Parse(args);
            if (options.SelfTest)
            {
                return LauncherContract.RunSelfTest();
            }

            string dataDirectory = !String.IsNullOrWhiteSpace(options.DataDirectory)
                ? LauncherContract.NormalizeDataDirectory(options.DataDirectory)
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SleepWell");
            string packageRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);

            if (options.SmokeTest)
            {
                if (String.IsNullOrWhiteSpace(options.DataDirectory)) return 31;
                return RunSmokeTest(packageRoot, dataDirectory);
            }

            bool createdNew;
            using (Mutex mutex = new Mutex(true, LauncherContract.GetMutexName(dataDirectory), out createdNew))
            {
                if (!createdNew)
                {
                    return OpenExistingInstance(dataDirectory, options.ResearchLab);
                }

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                using (HostController controller = new HostController(packageRoot, dataDirectory))
                using (MainWindow window = new MainWindow(controller, options.ResearchLab))
                {
                    Application.Run(window);
                }
                GC.KeepAlive(mutex);
            }
            return 0;
        }

        private static int OpenExistingInstance(string dataDirectory, bool researchLab)
        {
            string readyFile = Path.Combine(dataDirectory, "desktop-ready.json");
            try
            {
                ReadyMetadata candidate = LauncherContract.ReadReadyFile(readyFile);
                ReadyMetadata verified;
                if (!LauncherContract.TryReadAndVerify(readyFile, candidate.Pid, out verified))
                {
                    MessageBox.Show("실행 중인 SleepWell을 안전하게 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.", "SleepWell", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return 21;
                }
                LauncherContract.OpenApplicationUrl(verified.Url, researchLab);
                return 0;
            }
            catch
            {
                MessageBox.Show("실행 중인 SleepWell의 연결 정보를 읽지 못했습니다.", "SleepWell", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 22;
            }
        }

        private static int RunSmokeTest(string packageRoot, string dataDirectory)
        {
            bool createdNew;
            using (Mutex mutex = new Mutex(true, LauncherContract.GetMutexName(dataDirectory), out createdNew))
            {
                if (!createdNew) return 32;
                using (HostController controller = new HostController(packageRoot, dataDirectory))
                {
                    try
                    {
                        controller.StartAsync().GetAwaiter().GetResult();
                        ReadyMetadata verified;
                        if (!controller.VerifyCurrentReady(out verified)) return 33;
                        return controller.Shutdown() ? 0 : 35;
                    }
                    catch
                    {
                        controller.Shutdown();
                        return 34;
                    }
                }
            }
        }
    }
}
