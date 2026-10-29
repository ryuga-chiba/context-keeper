---
name: context-keeper
description: >
  会話のニュアンス(決定の理由・却下した案・ユーザーの訂正や好み・口頭の制約・用語・作業の途中状態)を
  プロジェクト内の作業ノートに退避し、コンテキスト圧縮(compact)の後に復元するスキル。
  「context-keeper:」で始まる checkpoint due 通知が出たとき、圧縮後に再開カードが注入されたとき、
  ユーザーが「今の文脈を残して」「チェックポイント」「圧縮前に保存」「前回の続き」「ノートを保存して」と言ったとき、
  `/context-keeper save|restore|status|rollup` が呼ばれたときに使用する。
  議事録や外部サービスへの保存、会話のまとめ資料づくりには使わない(このスキルの保存先はプロジェクト内のノートだけ)。
argument-hint: "[save|restore|status|rollup]"
allowed-tools:
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py path *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py saved *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py card *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py status *)
  - Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py rollup *)
  - Bash(python ${CLAUDE_SKILL_DIR}/scripts/ck.py path *)
  - Bash(python ${CLAUDE_SKILL_DIR}/scripts/ck.py saved *)
  - Bash(python ${CLAUDE_SKILL_DIR}/scripts/ck.py card *)
  - Bash(python ${CLAUDE_SKILL_DIR}/scripts/ck.py status *)
  - Bash(python ${CLAUDE_SKILL_DIR}/scripts/ck.py rollup *)
  - Edit(//**/.context-keeper/sessions/**)
  - Read(//**/.context-keeper/**)
---

# context-keeper

圧縮で消えやすい会話のニュアンスを `<project>/.context-keeper/` のノートに残し、圧縮後に戻す。
フックを入れていれば、通知・圧縮前の原文退避・圧縮後の再注入は自動で行われる。このスキルの役目は**ノートの中身を書くこと**。

## 最重要ルール

- 引数がない、または `save` のとき: 下の「save」を行う。`restore` / `status` / `rollup` はそれぞれの節に従う。
- 「context-keeper: … checkpoint due」という通知を受けたら、本文が読み込み済みでも、まず Skill ツールでこのスキルを引数 `save` で**呼び直す**。コマンドの事前許可は呼んだターンだけ有効なため。
  - **ターン終了時の通知**の場合: 保存してパスを1行で伝え、ターンを終える。新しい作業は始めない。ユーザーへの質問や確認待ちはそのまま残す。
  - **作業途中の通知**(「まもなく自動圧縮」)の場合: 保存してから、中断した作業に戻る。
- ノートに書くのは**要約では落ちるもの**。作業ログや経緯の羅列は書かない。
- ユーザーの訂正・好み・却下理由は、言い換えずに**原文に近い形**で残す。
- ノートは **5,000字以内**。超えたら理由の文を短くする。`[D-…]` のIDと決定の行は消さない。消してよいのは撤回済みの行だけ。8,000字を超えると `saved` が拒否する。
- CLAUDE.md や auto memory には自動で書かない。どのプロジェクトでも通用する好みを見つけたら、昇格するかどうかをユーザーに**提案するだけ**にする。

コマンド中の `python3` は、Windows で見つからなければ `python` に置き換える。Windows で許可を求められずに実行するには、PowerShell ツールではなく Bash ツール(Git Bash)で実行する。

## save

1. パスと次の決定IDを確認する。
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py path --project "${CLAUDE_PROJECT_DIR}" --session ${CLAUDE_SESSION_ID}
   ```
   表示される内容: `note:`(このセッションのノート)、`next decision id:`、`decisions:` と `raw:`(絶対パス)。
   `previous note:` が出た場合は、前セッションの続きの可能性がある。前のノートを読み、まだ有効な「ユーザーの訂正・好み」「制約」「用語」「却下した案」を引き継ぐ。その際、引き継ぎ元のノート名を1行添える。
2. `note:` のファイルを Write / Edit で作る、または更新する。既存のノートは読んでから**統合**する(追記を重ねない)。形式は次のとおり。
   ```markdown
   # 作業ノート
   ## 目的
   このセッションで何を達成しようとしているか(1〜3行)
   ## 決定と理由
   - [D-YYYYMMDD-xxxx-n] 決定 — 理由 — 却下: 代案(却下理由) (supersedes: 古いID)
   ## 却下した案
   - 案: 却下した理由(誰が・なぜ)
   ## ユーザーの訂正・好み
   - 「原文に近い引用」→ どう反映するか
   ## 制約
   - 期限・使ってはいけないもの・環境の前提など
   ## 用語
   - 用語: このセッションでの意味(言い換え禁止の語を含む)
   ## 現在の状態
   - 何がどこまで終わったか(ファイル名・コマンド・未コミットの変更)
   ## 次の一手
   - 次にやること(具体的に)
   ## 未解決
   - ユーザーに確認すべきこと、保留中の判断
   ```
   - 見出しは `## ` のこの9つを使う。中に `### ` の小見出しを置いてもよい。
   - 決定の行は「決定と理由」の節にだけ書き、IDは `next decision id` から連番で振る。決定を覆すときは、新しい決定に `(supersedes: 古いID)` を付ける。取り消すだけなら `- [新ID] 撤回: 理由 (supersedes: 古いID)` と書く。
   - 「現在の状態」「次の一手」は毎回最新に書き換える。古い状態は残さない。
3. 保存を登録する。サイズの検査と決定ログへのロールアップも同時に行われる。
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py saved --project "${CLAUDE_PROJECT_DIR}" --session ${CLAUDE_SESSION_ID}
   ```
   警告が出たら直して再実行する。最後に、ユーザーへ「ノートを保存した(パス)」と1行で伝える。

## restore

圧縮後や再開時に、細部が必要になったときに使う。

1. 再開カードを表示する。
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py card --project "${CLAUDE_PROJECT_DIR}" --session ${CLAUDE_SESSION_ID}
   ```
2. 必要に応じて、カード末尾に出る絶対パスからノート全文と決定ログ(decisions.md)を読む。
3. 正確な言い回しが要るときだけ、カード末尾の「対話原文」のパス(raw/)を Grep で検索する。全文は読まない。
4. 食い違ったときは、カード冒頭の優先順位に従う。
   - 保存後に会話が進んでいた場合(「反映していない」と書かれている): 保存後に変わった項目は圧縮要約を優先する。
   - そうでない場合: 訂正・却下・制約・用語・決定はカードを優先し、「現在の状態」「次の一手」は新しい会話を優先する。

## status / rollup

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py status --project "${CLAUDE_PROJECT_DIR}" --session ${CLAUDE_SESSION_ID}
python3 ${CLAUDE_SKILL_DIR}/scripts/ck.py rollup --project "${CLAUDE_PROJECT_DIR}"
```

`status` は次を表示する: コンテキスト使用率と自動圧縮ラインの基準、ノートの鮮度とサイズ、フックの登録状況。`hooks ... not installed` と出たら、下の「フックの導入」をユーザーに案内する(Claude 自身は実行しない)。

`rollup` は `saved` のたびに自動で実行される。処理内容は次のとおり。

- 「決定と理由」の行を ID ごとに `decisions.md` へまとめる
- 撤回された決定と、200件を超えた古い決定は `archive/YYYY-MM.md` に1行で移す
- 30日より古い `raw/` と `state/` を削除する

`decisions.md` は毎回ノートから再生成されるので、直接は編集しない。

## フックの導入(各自1回、任意)

フックを入れると、次が自動になる。

- 使用率 60% / 75% / 90% での保存通知(Stop)
- 圧縮直前の通知(PostToolBatch)
- 原文の退避と、手動 `/compact` の1回ブロック(PreCompact)
- 圧縮要約の保存(PostCompact)
- 再開カードの注入(SessionStart)
- 1M モデルの判定(PostModelSwitch)

フックは**ユーザーが自分のターミナルで実行する**。Claude は `~/.claude/settings.json` を書き換えない。

macOS / Linux / WSL:

```bash
python3 ~/.claude/skills/context-keeper/scripts/ck.py install-hooks --dry-run
python3 ~/.claude/skills/context-keeper/scripts/ck.py install-hooks
```

Windows PowerShell(`~` は展開されないので `$HOME` を使う):

```powershell
python "$HOME\.claude\skills\context-keeper\scripts\ck.py" install-hooks
```

- 解除: `uninstall-hooks`
- このプロジェクトだけ止める: `disable`(再開は `enable`)
- 動作確認: `--self-test`

## データの置き場所

`<project>/.context-keeper/`(git の worktree ではメインのチェックアウト側)

| パス | 内容 | git |
|---|---|---|
| `sessions/<日付>_<sid8>.md` | セッションごとの作業ノート | 無視 |
| `decisions.md` | 決定ログ | 共有したければコミット可 |
| `archive/YYYY-MM.md` | 撤回・古い決定 | 共有したければコミット可 |
| `raw/<session>.md` | 圧縮直前の対話原文と圧縮要約 | 無視 |
| `state/` | フックの内部状態 | 無視 |

`.claude/` は保護パスで、書くたびに許可を求められるため使わない。
