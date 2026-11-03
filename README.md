# Self-hosted agents sandbox

Harness locale per eseguire task di programmazione dentro [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox). Ogni task parte in un container isolato: l'harness copia il workspace, lancia un agente, esegue le verifiche e distrugge la sandbox.

Il progetto è uno scheletro. L'agente `scripted` basta per provare il ciclo senza chiavi. L'agente `coding-cli` avvia Claude Code o Codex dentro la sandbox.

## Ciclo di una run

1. Legge `tasks/<id>/task.yaml` e i file in `workspace/`.
2. Chiede a OpenSandbox un container con l'immagine del task.
3. Crea la workdir, copia i file e scrive `.harness/prompt.md`.
4. Esegue l'agente.
5. Esegue i check. Il loro esito decide se il task è passato.
6. Salva `runs/<timestamp>-<id>/report.json` e distrugge la sandbox.

## Requisiti

- Docker, usato da OpenSandbox per l'esecuzione locale
- Python 3.11+
- Un server OpenSandbox in ascolto, di solito su `localhost:8080`

## Server locale

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server
```

Il controllo di salute risponde su `http://127.0.0.1:8080/health`.

## Installazione

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Carica le variabili prima di una run reale:

```bash
set -a
source .env
set +a
```

`SANDBOX_API_KEY` serve solo se il server locale ha l'autenticazione attiva. `SANDBOX_USE_SERVER_PROXY=true` instrada comandi e file attraverso il server, che è la configurazione usata dal quick start di OpenSandbox quando il client non raggiunge gli indirizzi dei container.

## Comandi

```bash
agent-harness list
agent-harness validate
agent-harness run hello
agent-harness run scripted-fix
agent-harness run --all
```

`hello` e `scripted-fix` non usano un modello. `fix-fizzbuzz` usa Claude Code e richiede `ANTHROPIC_API_KEY` o `ANTHROPIC_AUTH_TOKEN`, più un'immagine con Node.js:

```bash
docker pull opensandbox/code-interpreter:v1.1.0
agent-harness run fix-fizzbuzz
```

`--keep` lascia la sandbox accesa a fine run. I report restano in `runs/` e quella directory non va versionata.

L'exit code è `0` se tutti i task eseguiti passano, `1` se un check fallisce o la run va in errore, `2` se il task o la configurazione non sono validi.

## Task

Un task è una directory con `task.yaml` e, di solito, `workspace/`.

```yaml
id: hello
title: Saluto dalla sandbox
image: python:3.12
workdir: /workspace
timeout_minutes: 10
command_timeout_seconds: 60
agent:
  kind: scripted
  commands:
    - python main.py
checks:
  - name: output
    command: python main.py
    expect_stdout_contains: hello
```

`command_timeout_seconds` deve stare dentro `timeout_minutes`: il primo limita il singolo comando, il secondo è il TTL della sandbox. `resources` accetta i limiti di OpenSandbox (`cpu`, `memory`) e viene inoltrato così com'è.

`workspace` può puntare a un'altra directory, come fa `scripted-fix` con il workspace di `fix-fizzbuzz`. I file indicati sotto `files` sono testo inline e sovrascrivono i file omonimi del workspace.

I check accettano `expect_exit_code` (default `0`) e `expect_stdout_contains`. Senza check, il task passa se tutti i comandi dell'agente escono con codice `0`.

## Agenti

`scripted` esegue `agent.commands` nella workdir. Si ferma al primo comando diverso da zero e poi lancia comunque i check.

`coding-cli` installa una CLI nel container e le passa il prompt.

| `provider` | Installazione | Variabili richieste |
| --- | --- | --- |
| `claude` | `npm i -g @anthropic-ai/claude-code@latest` | `ANTHROPIC_API_KEY` oppure `ANTHROPIC_AUTH_TOKEN` |
| `codex` | `npm i -g @openai/codex@latest` | `OPENAI_API_KEY` |
| `custom` | `agent.install`, se presente | `agent.required_env` |

L'immagine di default dei task di coding agent in questo repo è `opensandbox/code-interpreter:v1.1.0`, perché include Node.js. Un'immagine `python` non basta per Claude o Codex.

Claude viene lanciato con `--dangerously-skip-permissions` e Codex con `--ask-for-approval never`: la run non può fermarsi in attesa di un utente. Il confine di isolamento resta la sandbox OpenSandbox, non la sandbox interna della CLI. Codex usa `--sandbox workspace-write`, non il bypass completo della propria sandbox.

`agent.command` sostituisce il comando predefinito. Il segnaposto `{prompt_file}` diventa il percorso del prompt dentro la sandbox. `agent.install: []` salta l'installazione predefinita. `agent.env` elenca variabili aggiuntive da copiare dall'host; i valori vuoti non vengono inoltrati.

Esempio di provider custom:

```yaml
agent:
  kind: coding-cli
  provider: custom
  install:
    - pip install mio-agente
  command: mio-agente --prompt {prompt_file}
  required_env:
    - MIO_AGENTE_API_KEY
  env:
    - MIO_AGENTE_API_KEY
```

Per aggiungere un agente diverso da una CLI, estendi `resolve_plan` in `src/agent_harness/agents.py`. L'harness parla con la sandbox solo attraverso `SandboxSession`: `write_text`, `run` e `destroy`.

## Layout

```text
src/agent_harness/    loader, agenti, sessione OpenSandbox, ciclo dell'harness, CLI
tasks/                un task per directory
tests/                test del loader e dell'harness, con una sandbox finta
runs/                 report locali, ignorati da git
```

I test non avviano Docker:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

## Note operative

Le chiavi restano nell'ambiente dell'host. L'harness le inietta nel container solo per il provider del task e non le scrive nel report. Non montare la home dell'host dentro la sandbox: i comandi dell'agente sono codice non fidato e il container è il confine.

Il server OpenSandbox va tenuto spento quando non serve. `--keep` e un timeout lungo lasciano container in esecuzione fino al TTL o a una distruzione esplicita.
