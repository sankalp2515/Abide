# intentseal-core

The engine behind [IntentSeal](https://pypi.org/project/intentseal/), a prompt-injection firewall for AI agents:
content extraction for every format (PDF, Word, HTML, email, Markdown, JSON, code, images via OCR), normalisation and
decoding, a three-tier detector (structural rules, a local classifier, an AI judge), session risk and data-flow
tracking, the action guard and the decision log.

Most users install the SDK instead:

```bash
pip install intentseal                 # SDK, remote mode
pip install "intentseal[embedded]"     # SDK with this engine in-process
```

Extras of this package: `engine` (extraction, normalisation, secret scan), `classifiers` (local ONNX classifier),
`ocr`, `postgres`, `anthropic`, `all`.

It also installs the `intentseal` command: `intentseal init` writes a starter policy; `intentseal providers check`
lists the configured model providers and tests each key (key values are never printed).

Source and documentation: https://github.com/sankalp2515/intentseal · Licensed under the Apache License 2.0.
