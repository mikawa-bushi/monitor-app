# Security Policy

## Supported versions

Security fixes are made for the latest release line only.

| Version | Supported |
|---------|-----------|
| 2.5.x   | ✅        |
| < 2.5   | ❌        |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately instead:

- **GitHub**: open the repository's **Security** tab and choose **Report a vulnerability**
  (private vulnerability reporting), or
- **Email**: **tomatosimple1104@gmail.com**

Include as much of the following as you can:

- the affected version (`monitor-app --version`) and how it is installed and deployed
  (pipx / uv / pip / Docker, reverse proxy or not, database)
- the steps or a request that reproduces the problem
- what an attacker can do with it (the impact)

Remove real passwords, API keys and personal data from what you send. You will get a reply
when the report has been looked at, and the fix will be announced in
[CHANGELOG.md](CHANGELOG.md) once a fixed release is published.

## 脆弱性の報告(日本語)

セキュリティの問題は**公開の issue に書かず**、上記の GitHub の **Security** タブの
**Report a vulnerability**、またはメール(**tomatosimple1104@gmail.com**)で非公開で
報告してください。影響を受ける版・再現の手順・影響の範囲を添えてください(本物の
パスワード・API キー・個人情報は除く)。修正版を公開したら [CHANGELOG.md](CHANGELOG.md)
でお知らせします。
