# Encrypted transcript archive

Daily Bloomberg Show, Bloomberg Top Videos, and ARK Invest transcripts are retained here as encrypted CMS files. No plaintext transcript, subtitle, prompt, or private key belongs in this directory.

```text
transcripts/
  shows/YYYY-MM-DD/<source-hash>__<transcript-hash>.json.cms
  shows/YYYY-MM-DD/<source-hash>__<transcript-hash>.md.cms
  top-videos/YYYY-MM-DD/<source-hash>__<transcript-hash>.json.cms
  top-videos/YYYY-MM-DD/<source-hash>__<transcript-hash>.md.cms
  ark-invest/YYYY-MM-DD/<source-hash>__<transcript-hash>.json.cms
  ark-invest/YYYY-MM-DD/<source-hash>__<transcript-hash>.md.cms
```

Each pair contains the same normalized transcript in machine-readable JSON and readable Markdown. Filenames contain only canonical SHA-256 identifiers, not titles or transcript text.

## Encryption profile

- Container: DER CMS `AuthEnvelopedData`
- Content encryption: AES-256-GCM
- Recipient key: RSA-3072
- Key transport: RSA-OAEP with SHA-256 and MGF1-SHA256
- Recipient identification: X.509 Subject Key Identifier
- Public certificate: `config/transcript-archive-recipient-v1.pem`
- Certificate SHA-256 fingerprint: `E3:D1:76:F7:B0:C0:90:C8:D4:85:57:C4:B5:CC:3D:94:AC:96:F6:C9:9B:9E:0E:BF:7D:F6:F3:7E:26:02:BD:52`

The matching private key is intentionally absent from the repository, GitHub Secrets, Actions artifacts, and logs. Certificate rotation must use a new versioned certificate while retaining the old local private key for historical archives.

GitHub Actions necessarily handles plaintext during transcription and planning. Only authenticated ciphertext is retained in `main` and Git history.
