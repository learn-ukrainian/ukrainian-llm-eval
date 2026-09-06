# Ukrainian LLM Eval

Reproducible evaluation of **Ukrainian language** models and agents, with optional reference tools.

This project is under development toward its first release. It does not yet publish a complete model leaderboard. Follow the [release epic](https://github.com/learn-ukrainian/ukrainian-llm-eval/issues/1).

The engine supports ZNO/NMT multiple-choice and matching questions, ULP proficiency questions, and UA-GEC correction with separate reference custody, preserved execution evidence, native Claude and Kimi execution, and compatible chat-completions endpoints. UA-GEC scoring uses a separately built offline Docker runtime. Additional native adapters and the public evaluation remain tracked work. No other exam subjects are planned.

## Development installation

Requires Python 3.11 or newer. Validation targets Linux and macOS; Windows behavior is unverified. From a clean clone:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ukrainian_llm_eval --help
.venv/bin/python -m pytest
.venv/bin/python -m ruff check src tests
.venv/bin/python -m build
```

Provider runs require your own authorized CLI subscription or endpoint credentials. Running tests does not invoke paid models. Do not commit credentials, grading keys, raw provider logs or retrievals.

The [Kimi runbook](docs/native-kimi.md) explains private subscription provisioning
and execution with or without Sources MCP. Kimi results identify the requested
subscription route; its CLI does not attest the underlying backend model.
The [Responses API runbook](docs/responses-http.md) covers DeepSeek's stateless
HTTP route, including complete tool history and reasoning-inclusive output caps.

## Evaluation boundaries

A candidate receives questions, while the separate scorer receives the answer key. Hiding a key does not prove public questions were unseen during training. Live Sources MCP is optional; changing reference content limits exact reproducibility. The evaluator does not host, back up or publish that service.

Scores concern the selected tasks and datasets. They do not certify overall fluency or a CEFR level. Requested effort and verified effective effort are distinct, and unsupported tool access is recorded rather than simulated.

See [benchmark preparation and scoring](docs/benchmarks.md), [running an exam](docs/running.md), [segmented research execution](docs/research-scheduling.md), [the delivery plan](docs/delivery-plan.md), [contributing](CONTRIBUTING.md), [security](SECURITY.md) and [release procedure](docs/releasing.md). Code is MIT licensed; datasets and reference material retain their own rights.
