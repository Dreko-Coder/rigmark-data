// Conformance check: verifies a price file the way the Rigmark app does (Rigmark/Rig/Pricing/PriceFile.cs).
// Same envelope and payload shapes, same JSON options (camelCase, unknown fields refused), same signing bytes
// ("Rigmark.PriceFile.v1\n" + payload), same key import (SubjectPublicKeyInfo DER, P-256 only) and the same
// signature call (SHA-256, DSASignatureFormat.Rfc3279DerSequence). Used by tests/test_signing.py.
//
// Usage: PriceFileCheck <price-file.json> <public-key-der-base64> <key-id>
// Exit 0 and "verified sequence N bands B indices I" when the file verifies; exit 1 with the reason otherwise.
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

try
{
    byte[] envelopeBytes = File.ReadAllBytes(args[0]);
    byte[] publicKey = Convert.FromBase64String(args[1]);
    string trustedKeyId = args[2];
    var codec = new JsonSerializerOptions
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        MaxDepth = 6,
    };
    var envelope = JsonSerializer.Deserialize<Envelope>(envelopeBytes, codec) ?? throw new InvalidDataException("empty envelope");
    if (envelope.SchemaVersion != 1 || envelope.KeyId != trustedKeyId) throw new InvalidDataException("schema or key id not trusted");
    byte[] payload = Convert.FromBase64String(envelope.PayloadBase64), signature = Convert.FromBase64String(envelope.SignatureBase64);
    if (signature.Length is < 8 or > 80) throw new InvalidDataException("signature outside its bounds");

    byte[] prefix = Encoding.ASCII.GetBytes("Rigmark.PriceFile.v1\n"), message = new byte[prefix.Length + payload.Length];
    prefix.CopyTo(message, 0); payload.CopyTo(message.AsSpan(prefix.Length));

    using var verifier = ECDsa.Create();
    verifier.ImportSubjectPublicKeyInfo(publicKey, out int consumed);
    if (consumed != publicKey.Length || verifier.KeySize != 256 || verifier.ExportParameters(false).Curve.Oid?.Value != "1.2.840.10045.3.1.7")
        throw new InvalidDataException("public key is not a P-256 SubjectPublicKeyInfo");
    if (!verifier.VerifyData(message, signature, HashAlgorithmName.SHA256, DSASignatureFormat.Rfc3279DerSequence))
        throw new InvalidDataException("signature check failed");

    var data = JsonSerializer.Deserialize<Payload>(payload, codec) ?? throw new InvalidDataException("empty payload");
    if (data.IssuedUtc.Kind != DateTimeKind.Utc) throw new InvalidDataException("issuedUtc is not UTC");
    if (data.Bands.Any(b => b.Sellers < 3) || data.Indices.Any(i => i.Sellers < 2)) throw new InvalidDataException("seller minimum broken");
    Console.WriteLine($"verified sequence {data.Sequence} bands {data.Bands.Length} indices {data.Indices.Length}");
    return 0;
}
catch (Exception e) when (e is InvalidDataException or JsonException or CryptographicException or FormatException)
{
    Console.WriteLine($"refused: {e.GetType().Name}: {e.Message}");
    return 1;
}

internal sealed record Envelope(int SchemaVersion, string KeyId, string PayloadBase64, string SignatureBase64);
internal sealed record RegionRow(string Region, string Currency, decimal TaxRatePercent, bool PricesIncludeTax);
internal sealed record IndexRow(string Region, string Category, decimal Multiplier, decimal Low, decimal High, int Sellers, int Sample, string DataDate, bool Seeded);
internal sealed record BandRow(string PartId, string Region, string Currency, decimal Median, decimal Min, decimal Max, int Sellers, string DataDate);
internal sealed record Payload(
    int SchemaVersion, long Sequence, DateTime IssuedUtc, string DataDate, string FxBase,
    Dictionary<string, decimal> FxRates, RegionRow[] Regions, IndexRow[] Indices, BandRow[] Bands);
