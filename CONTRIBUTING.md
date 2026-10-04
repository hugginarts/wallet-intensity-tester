# Contributing

Thanks for helping improve Wallet Intensity. Open an issue first for larger changes so the scope can be discussed.

## Before submitting

- Keep the tool read-only. Do not add private-key collection, signing, or automatic order execution.
- Never commit RPC URLs, API keys, wallet exports, private datasets, or screenshots that expose credentials.
- Describe data coverage and assumptions when changing PnL or transaction parsing.
- Run the checks below and include the result in your pull request.

```bash
python -m py_compile wallet_intensity_v2.py
python -m unittest discover -s tests -v
```

Pull requests are reviewed before they are merged.
