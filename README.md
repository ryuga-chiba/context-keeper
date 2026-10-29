# context-keeper

Claude Code のコンテキスト圧縮(compact)で消えやすい会話のニュアンスを、プロジェクト内の作業ノートに退避し、圧縮のあとに自動で読み戻すスキルです。

> **Status: 実機検証前の開発版です。** 自己テストと、実際の会話ログのコピーを使ったフックの呼び出し確認は済んでいますが、
> 本物の Claude Code での通しの確認(通知 → 保存 → 圧縮 → 再開カード)はまだです。詳しくは [状態](#状態) を参照してください。

**English summary:** A Claude Code skill that saves the nuance a compaction summary tends to drop
(decisions and their reasons, rejected options, your corrections, informal constraints, terms, in-progress state)
into a per-project note, then re-injects a short "resume card" after compaction. It never writes to `CLAUDE.md`.
Python 3 standard library only. Docs are in Japanese.

## 何が消えるのか

圧縮した要約は「何をしたか」(依頼、ファイル、エラーとその直し方、残りのタスク)を残しますが、次のものは落ちやすくなります。

- 決定の理由と、比べた代案
- 却下した案と、その理由
- あなたの訂正や好み(「X ではなく Y と呼ぶ」など)
- 口頭で伝えた制約(期限、使わないもの)
- 途中の判断と、未解決の質問

このスキルは、圧縮の前にそれらをノートに書き、圧縮の直後に要点を「再開カード」として自動で読み込ませます。

## 仕組み

圧縮そのものは Claude Code 本体が行い、スキルから `/compact` は実行できません。そこで、圧縮の前後にフックで介入します。
ノートの中身を書くのは Claude(スキル本体)、通知・退避・読み戻しはモデルに頼らないフックが担当します。

| タイミング | 起きること |
|---|---|
| 使用率 60% / 75% / 90%(ターンの終わり) | ノートが古ければ、Claude に保存を促し、ユーザーにも通知する |
| 残り 10%(作業の途中) | 長い自律作業の途中で圧縮されそうなときだけ、一度保存を促す |
| 圧縮の直前 | あなたの発言と Claude の返答の原文を `raw/` に退避する。手動の `/compact` は、ノートが古ければ**一度だけ**止める(10分以内にもう一度実行すれば、そのまま圧縮される) |
| 圧縮の直後 | 圧縮要約を `raw/` に保存し、通知の状態をリセットする |
| 再開の最初 | ノートの要点を優先度順に詰めた再開カード(約6,000字相当まで)を注入する。保存後に会話が進んでいた場合は、カードより新しい要約を優先するよう書き添える |

使用率の「100%」は、自動圧縮が起きるラインです。1M コンテキストのモデルは約 967K トークン、200K のモデルは 200K、`/autocompact` などで設定した場合はその値を基準にします。

## インストール

必要なもの: Python 3(標準ライブラリのみ。動作確認は 3.12)、Claude Code。

### 1. スキル本体

`~/.claude/skills/context-keeper` に clone します。

```bash
git clone https://github.com/ryuga-chiba/context-keeper ~/.claude/skills/context-keeper
```

Windows PowerShell:

```powershell
git clone https://github.com/ryuga-chiba/context-keeper "$HOME\.claude\skills\context-keeper"
```

これで `/context-keeper save` などのコマンドが使えます。

### 2. フック(任意・1回)

フックを入れると、保存の通知・原文の退避・圧縮後の再開カードが自動になります。入れなくても、手動の `save` と `restore` は使えます。
自分のターミナルで実行してください。`~/.claude/settings.json` の既存の設定は保持され、書き換える前にバックアップが作られます。
`--dry-run` を付けると、書き換え後の内容を表示するだけで終わります。

```bash
python3 ~/.claude/skills/context-keeper/scripts/ck.py install-hooks --dry-run
python3 ~/.claude/skills/context-keeper/scripts/ck.py install-hooks
```

Windows PowerShell(`~` は展開されないため `$HOME` を使います):

```powershell
python "$HOME\.claude\skills\context-keeper\scripts\ck.py" install-hooks --dry-run
python "$HOME\.claude\skills\context-keeper\scripts\ck.py" install-hooks
```

別の場所に clone した場合も、その場所の `scripts/ck.py` で同じように実行できます。

| やりたいこと | コマンド(`ck.py` のあとに付ける) |
|---|---|
| フックを外す | `uninstall-hooks` |
| このプロジェクトだけ止める | `disable`(再開は `enable`) |
| 動作確認 | `--self-test` |

フックの登録は `settings.json` に書き込みます。WSL 上の Claude デスクトップ版ではプラグインが使えないため、プラグインではなくこの方式にしています。

## 使い方

フックを入れていれば、普段は何もする必要がありません。手動では次のコマンドを使います。

| コマンド | 内容 |
|---|---|
| `/context-keeper save` | 今の会話のニュアンスをノートに保存する(引数なしでも同じ) |
| `/context-keeper restore` | 再開カードとノートを読み戻す。細部が要るときだけ原文を検索する |
| `/context-keeper status` | 使用率、ノートの鮮度、フックの登録状況を表示する |
| `/context-keeper rollup` | 決定ログの整理を手動で実行する(保存のたびに自動でも実行される) |

言葉で頼んでもかまいません: 「今の文脈を残して」「圧縮前に保存して」「前回の続きから」。

## ノートと保存先

保存先は `<project>/.context-keeper/` です(git の worktree では、メインのチェックアウト側)。
`.claude/` は Claude Code の保護パスで、書き込むたびに許可を求められるため使いません。

| パス | 内容 | git |
|---|---|---|
| `sessions/<日付>_<sid8>.md` | セッションごとの作業ノート | 無視 |
| `decisions.md` | 決定ログ(ノートから自動生成) | 共有したければコミット可 |
| `archive/YYYY-MM.md` | 撤回された決定・古い決定 | 共有したければコミット可 |
| `raw/<session>.md` | 圧縮直前の対話原文と圧縮要約 | 無視 |
| `state/` | フックの内部状態 | 無視 |

「無視」の項目は、スキルが作る `.context-keeper/.gitignore` で git の管理外になります。プロジェクト直下の `.gitignore` は変更しません。

ノートは「目的 / 決定と理由 / 却下した案 / ユーザーの訂正・好み / 制約 / 用語 / 現在の状態 / 次の一手 / 未解決」の9節です。
`CLAUDE.md` には書き込みません。増え続けないように、次の上限があります。

- ノートは 5,000 字が目安。8,000 字を超えると保存が拒否されます。
- `decisions.md` は 200 件を超えたら新しい 150 件を残し、古い決定と撤回済みの決定は `archive/` に1行で移します。
- `raw/` と `state/` は 30 日で削除されます。

`decisions.md` は保存のたびにノートから再生成されます。直接直さず、ノートの該当行を直してください。

## 設計上の判断

- 圧縮そのものは本体に任せる。スキルから `/compact` は実行できない。
- 使用率はフックが会話ログ(transcript)のトークン数から計算する。Claude 自身は使用率を正確に知らない。
- 再注入は `SessionStart`(`compact`)で行う。`PostCompact` の出力はコンテキストに入らない。
- フックは失敗しても会話を止めない。例外や想定外の入力では何もせず終了し、止めるのは手動 `/compact` の1回だけ。
- フックは Python の分離モード(`-I`)で起動し、`ck.py` が見つからなくても exit 0 で終わる。

これらは Claude Code の公式ドキュメント(hooks、skills、context-window、permission-modes など)で確認しています。

## 状態

- 済み: スキル本体・フック・導入コマンドの実装、自己テスト約 90 項目(Python 3.12)、実際の会話ログのコピーを使った全フックの呼び出し確認、複数エージェントによるレビュー2回(確認された指摘は修正済み)。
- 未確認: Claude デスクトップ版 v2.1.286 での実機の通し確認、Windows と macOS での確認、最後の修正後の再レビュー。
- 既知の制約:
  - Windows で PowerShell ツールから実行すると、毎回許可を求められます(Git Bash の利用を前提にしています)。
  - 使用率の基準は推定です。起動時だけの `--autocompact` は、最初の自動圧縮で学習するまで見えません。

不具合や要望は Issue か Pull Request でお知らせください。

## 資料

[docs/context-keeper-guide.pptx](docs/context-keeper-guide.pptx) に、仕組みと使い方をまとめたスライド(12枚)があります。

## ライセンス

[MIT](LICENSE)
