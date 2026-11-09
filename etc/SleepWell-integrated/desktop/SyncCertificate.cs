using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;

// Local certificate generation only. No machine certificate store or admin changes.
internal static class SyncCertificate
{
    static byte[] Element(byte tag, byte[] value)
    {
        byte[] size;
        if (value.Length < 128) size = new byte[] { (byte)value.Length };
        else {
            var bytes = BitConverter.GetBytes(value.Length).Reverse().SkipWhile(b => b == 0).ToArray();
            size = new byte[] { (byte)(128 | bytes.Length) }.Concat(bytes).ToArray();
        }
        return new byte[] { tag }.Concat(size).Concat(value).ToArray();
    }
    static byte[] Integer(byte[] value)
    {
        value = value.SkipWhile(b => b == 0).ToArray();
        if (value.Length == 0) value = new byte[] { 0 };
        if (value[0] >= 128) value = new byte[] { 0 }.Concat(value).ToArray();
        return Element(2, value);
    }
    static void Pem(string path, string label, byte[] bytes)
    {
        using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write))
        using (var writer = new StreamWriter(stream, Encoding.ASCII)) {
            writer.WriteLine("-----BEGIN " + label + "-----");
            writer.WriteLine(Convert.ToBase64String(bytes, Base64FormattingOptions.InsertLineBreaks));
            writer.WriteLine("-----END " + label + "-----");
        }
    }
    static int Main(string[] args)
    {
        try {
            if (args.Length == 2 && args[0] == "--check") {
                string pem = File.ReadAllText(Path.Combine(Path.GetFullPath(args[1]), "sync-cert.pem"));
                string encoded = pem.Replace("-----BEGIN CERTIFICATE-----", "")
                                    .Replace("-----END CERTIFICATE-----", "").Trim();
                using (var certificate = new X509Certificate2(Convert.FromBase64String(encoded))) {
                    return certificate.NotBefore.ToUniversalTime() <= DateTime.UtcNow &&
                           certificate.NotAfter.ToUniversalTime() > DateTime.UtcNow.AddDays(7) ? 0 : 4;
                }
            }
            if (args.Length != 1) return 2;
            string directory = Path.GetFullPath(args[0]);
            Directory.CreateDirectory(directory);
            string certPath = Path.Combine(directory, "sync-cert.pem");
            string keyPath = Path.Combine(directory, "sync-key.pem");
            if (File.Exists(certPath) || File.Exists(keyPath)) return 3;
            using (var rsa = new RSACng(2048)) {
                var request = new CertificateRequest("CN=SleepWell Local Sync", rsa,
                    HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
                request.CertificateExtensions.Add(new X509BasicConstraintsExtension(false, false, 0, true));
                request.CertificateExtensions.Add(new X509KeyUsageExtension(X509KeyUsageFlags.DigitalSignature |
                    X509KeyUsageFlags.KeyEncipherment, true));
                var purposes = new OidCollection();
                purposes.Add(new Oid("1.3.6.1.5.5.7.3.1"));
                request.CertificateExtensions.Add(new X509EnhancedKeyUsageExtension(purposes, true));
                using (var cert = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1),
                                                          DateTimeOffset.UtcNow.AddYears(3))) {
                    var p = rsa.ExportParameters(true);
                    byte[] der = Element(48, new byte[][] { new byte[] { 0 }, p.Modulus,
                        p.Exponent, p.D, p.P, p.Q, p.DP, p.DQ, p.InverseQ }
                        .SelectMany(Integer).ToArray());
                    Pem(keyPath, "RSA PRIVATE KEY", der);
                    Pem(certPath, "CERTIFICATE", cert.Export(X509ContentType.Cert));
                    Array.Clear(der, 0, der.Length);
                }
            }
            return 0;
        } catch { return 1; }
    }
}
