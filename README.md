# Auto Issue

An LLM powered GitHub Action that triages new issues and pull requests:

- closes and locks **spam**, malicious or useless content;
- answers questions that the README or a pinned issue already covers, then closes them;
- asks for details when a bug report is too vague;
- labels everything that is legitimate.


## Quick start

```yaml
name: Triage
on:
  issues:
    types: [opened]
  pull_request_target:
    types: [opened]

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  triage:
    runs-on: ubuntu-latest
    steps:
      - uses: ilovescratch2/action-auto-issue@main
        with:
          ai-base-url: https://api.openai.com/v1
          ai-key: ${{ secrets.OPENAI_API_KEY }}
          labels: bug,enhancement,question
```


## Requirements

- `contents: read` to read the README, `issues: write` and `pull-requests: write` to comment,
  label, close and lock.
- An OpenAI compatible endpoint and its API key.

## Choosing a provider

| Provider | `ai-base-url` | `model` |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI Compatible | `http://<host>/v1` | Model |

You can set `ai-api-type: responses` to use the responses API.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `token` | `${{ github.token }}` | Token for the repository API. |
| `ai-base-url` | No (**required**) | OpenAI compatible base URL. |
| `ai-key` | No (**required**) | API key, keep it in a secret. |
| `model` | `gpt-4o` | Model used for every check. |
| `ai-api-type` | `chat-completions` | `chat-completions` or `responses`. |
| `labels` | `bug,enhancement,question` | Labels the AI may apply. |
| `apply-labels` | `true` | Write labels at all, both the AI's pick and the `outcomes` labels. |
| `search-history` | `false` | Search past issues and pull requests for duplicates and answering material. |
| `language` | `en` | `en` or `zh-CN`: the comments the bot writes, and the fallback for `answer-language`. |
| `answer-language` | `auto` | Language of the answers the model writes: `auto`, `locale`, `en`, `zh` or `both`. |
| `issue-code-access` | `off` | `off`, `read` (the project files inform the checks) or `advise` (also post a diagnosis and fix suggestion). |
| `pr-code-access` | `patch` | `off`, `patch` (the changed lines) or `full` (the whole diff plus the changed files). |
| `analysis-depth` | `normal` | `light` / `normal` / `deep` |
| `pr-review` | `off` | `on` posts an AI code review of the diff as a review comment. Never an approval, and drafts are skipped. |
| `blocked-users` | — | Logins closed and locked without any AI call. |
| `max-tokens` | `256` | Upper bound per model response. |
| `content-max-chars` | `20000` | Truncation limit |
:

| Decision | Result |
| --- | --- |
| Spam | comment, close (`not_planned`), lock |
| Duplicate of a recent issue | comment with the link, keep open (unless `issue_duplicate` says otherwise) |
| Covered by the documentation | answer from the README/pinned issues, close (`completed`), **no lock**, so the author can still reply |
| Basic usage question | comment, close, lock |
| Too vague | answer from the README when possible, otherwise ask for details, keep open |
| Valid | apply the matching label, keep open |

**Pull requests** (`pull_request_target.opened`) run spam check → commit title check → quality check.
Spam, a non descriptive title, malicious content and trivial changes are closed and locked; everything
else gets labelled and, with `pr-review: on`, reviewed.

### Code

By default the issue checks see only the README, the pinned issues and the issue text, and pull
requests are summarised by the changed lines of their patch. Both can be widened:

| `issue-code-access` | Behaviour |
| --- | --- |
| `off` (default) | The issue checks never read project files. |
| `read` | After the spam verdict the model is shown the repository file list, names the files worth reading, and those files join the prompts of the coverage, quality and classification checks. It is told not to suggest a fix. |
| `advise` | The same, plus: when the issue is kept open as a valid report, one more call diagnoses it and the resulting fix suggestion is posted as a comment. |

| `pr-code-access` | Behaviour |
| --- | --- |
| `off` | The diff is not fetched at all; the checks see the title and description only. |
| `patch` (default) | The changed lines of each file, as before. |
| `full` | The whole patch (hunk headers and context lines included) plus the full content of the changed files at the head commit. |

Reading files costs API requests and prompt tokens, so every knob has a budget: `config.json`'s
`code_access` section holds the caps (`max_files_to_read`, `max_tree_files`, `max_chars_per_file`,
`max_total_chars`, `diff_lines`). The file list skips vendored, generated and binary paths, and the
model may only pick paths that were actually offered, so it cannot make the action fetch anything
arbitrary.

`advise` writes prose rather than a verdict, so it uses its own output budget
(`code_access.advise_max_tokens`, 700 by default) instead of `max-tokens`.

### Review

`pr-review: on` adds a fourth stage to the pull request path: once the three checks pass and the
pull request is not a duplicate, the model is asked to review the change itself (functional fit,
minimal change, backward compatibility, security, code quality) and the report is posted as a
**review comment**. The action never approves, never requests changes and never closes on the
review's verdict: the report carries a conclusion for the maintainer to act on.

The report is written in the language `answer-language` resolves to, and framed by
`pr_review_prefix` (`config.json`, overridden by `locales/*.json`), which names the model that
wrote it.

The reviewer is not limited to the diff. It may answer with `REQUEST_FILES:` and a list of paths
instead of a report, and the action then reads those files **at the pull request's head commit**,
adds them to the next call and asks again — so it can follow the change into its callers, types and
tests. The loop is bounded by `config.json`'s `review` section:

| Key | Default | Meaning |
| --- | --- | --- |
| `max_rounds` | `2` | How many times the reviewer may ask for more files. |
| `max_files` | `10` | Files read across all rounds. |
| `max_chars_per_file` | `8000` | Per file cap. |
| `max_total_chars` | `48000` | Cap for everything read. |
| `max_diff_files` | `20` | Changed files the reviewer sees, independent of `analysis-depth`. |
| `max_diff_lines` | `40` | Patch lines per file, over the `analysis-depth` limit. |
| `max_commits` | `30` | Commit messages sent with the diff. |
| `max_tokens` | `3000` | Output budget, since a report is long prose. |

Only paths that were actually offered in the file list are read, so the reviewer cannot make the
action fetch anything arbitrary. The stage costs a few extra API reads (the diff again, the commit
list, the file list, and one request per file it reads) and one model call per round.

It is skipped for drafts, and it needs a `pr-code-access` other than `off`: rather than fetching
code the repository asked to keep away from the AI, it says so with a warning. A review that fails
(provider error, unusable answer, content filter) is logged and skipped — unlike the triage checks
it never closes the pull request and never fails the run. The verdict is printed to the log and
written to the job summary.

### History

With `search-history: true` AI also searches past issues and pull requests of the same
repository (one search request, after the spam verdict so a spam flood cannot burn the search
quota). The results are used twice:

- as answering material, on the same footing as the README and the pinned issues, so a question
  that an earlier discussion already answered is answered and closed instead of re-triaged;
- as a duplicate check: the model may point at one of the found items, and the bot then comments a
  link to it. Whether that comment closes the issue is up to the `issue_duplicate` / `pr_duplicate`
  outcome, and the number is validated against what was actually found, so the model cannot invent one.

Items carrying the tag `no-search` (`config.json` `history.exclude_label`) are excluded from the
results, which is the way to keep a discussion out of the answering material. The query is built
from the title's terms, so a title of one or two letter words finds nothing. The search endpoint has
its own rate limit (30 requests per minute for an authenticated token) and its index lags behind by
a few seconds, so an item opened moments ago will not show up.

### Outcome behaviour

What happens for each outcome is configuration, not code: `config.json`'s `outcomes` section gives every
outcome a `comment` / `close` / `lock` triple plus a list of fixed `labels`.

| Outcome | comment | close | lock |
| --- | --- | --- | --- |
| `issue_blocked`, `issue_spam`, `issue_basic` | ✓ | ✓ | ✓ |
| `issue_readme_covered` | ✓ | ✓ (`completed`) | |
| `issue_content_filtered` | ✓ | ✓ | |
| `issue_unclear` | ✓ | | |
| `pr_blocked`, `pr_spam`, `pr_invalid_commit`, `pr_malicious`, `pr_trivial`, `pr_content_filtered` | ✓ | ✓ | ✓ |

Set `close: false` on an outcome to triage without closing anything, or `comment: false` to close
silently. Four details worth knowing:

- `comment` governs the fixed outcome text only. Answers written by the model, and the duplicate and
  fix-suggestion comments, are separate and always post.
- `issue_unclear` with `comment: false` skips the smart-answer model call too, so nothing is paid for
  a comment nobody will read. Its `close`/`lock` apply when the bot falls back to asking for details,
  not when it managed to answer from the documentation.
- `labels` are added when the outcome happens, and every one of them must already exist in the
  repository: nothing is created and a missing label only warns.
- The lock reason is global (`config.json` `defaults.lock_reason`, default `spam`), so a locked PR
  shows the same reason regardless of the outcome.


## Customising

- `config.json` — every prompt, the response texts, the log lines and the model settings. Its
  `answer_languages` section holds the instruction the model sees for each `answer-language` value;
  the `auto` and `locale` phrases may use a `{language}` placeholder, which is filled with the
  locale's own language name.
- `locales/en.json`, `locales/zh-CN.json` — the user facing comments, the answer language name and
  the answer length limits. `language` accepts `en` and `zh-CN`. Supporting a third locale means
  adding the file and extending `SUPPORTED_LANGUAGES` and `normalize_language` in
  `src/auto_issue/config.py`; the `responses` keys of the locale files and `config.json` must stay in
  step, which the contract tests enforce.
- The prompt template and the untrusted input instruction are deliberately English only: they are
  machine facing, and keeping them stable makes the verdict parsing predictable.

## License

[Mozilla Public License v2.0](LICENSE)
