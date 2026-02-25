# ngrok で公開する（Render の代わりに）

ローカルで動かしているアプリを、インターネットからアクセスできるようにする方法です。Render のようにサーバーにデプロイするのではなく、**自分のPCで起動したアプリを ngrok のトンネルで外に公開**します。

## 前提

- アプリは **port 8001** で起動します（`python main.py` または `uvicorn main:app --host 0.0.0.0 --port 8001`）
- メモリ制限は **自分のPCのメモリ** なので、512MB 制限はありません
- Firebase（Firestore）もローカルからそのまま利用できます

## 手順

### 1. ngrok を用意する

- **無料で使う**: https://ngrok.com でアカウントを作り、ngrok をダウンロード
- または: `choco install ngrok`（Windows） / `brew install ngrok`（Mac）

### 2. アプリを起動する

```bash
cd D:\app\newsite
python main.py
```

`Uvicorn running on http://0.0.0.0:8001` と出れば OK です。

### 3. ngrok で 8001 を公開する

**別のターミナル**で:

```bash
ngrok http 8001
```

表示された **Forwarding** の URL（例: `https://xxxx-xx-xx-xx-xx.ngrok-free.app`）が、インターネットからアクセスできるアドレスです。

### 4. アクセスする

ブラウザでその URL を開くと、ローカルで動いているニュースサイトが表示されます。

- トップ: `https://xxxx.ngrok-free.app/`
- デバッグ: `https://xxxx.ngrok-free.app/debug`

## 注意点

- **PCを消したりスリープにすると** トンネルも止まり、外からはアクセスできません
- 無料の ngrok では **URL が起動のたびに変わります**（有料だと固定ドメインも可能）
- ローカルで Firestore を使う場合は、`.env` や `credentials/firebase-service-account.json` を設定しておいてください

## Render と ngrok の違い

| | Render | ngrok |
|---|--------|--------|
| 動かす場所 | Render のサーバー | 自分のPC |
| メモリ | 512MB 等の制限あり | PCのメモリ |
| 24時間 | 無料だとスリープあり | PCを付けっぱなしなら可 |
| 公開URL | 固定（例: xxx.onrender.com） | 無料なら毎回変わる |

「Render じゃなくて ngrok にする」＝ **デプロイ先を Render にせず、自PC + ngrok で公開する** という意味で、そのまま上記の手順で実現できます。
