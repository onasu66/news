# NFC Music Site Mock

NFCカードに書き込むURLから開く、カード別の音楽プレイヤーサイトのモックです。

## Local Preview

`index.html` をブラウザで開くだけでも確認できます。

おすすめは簡易サーバーでの確認です。

```bash
python -m http.server 5173
```

Then open:

```text
http://localhost:5173/c/card001
http://localhost:5173/c/card002
http://localhost:5173/c/card003
```

If your local server does not support fallback routing, use:

```text
http://localhost:5173/?card=card001
```

## Card Data

Cards are defined in:

```text
data/cards.json
```

Replace `audioUrl` and `coverUrl` with your real files when ready.

## Deploy Notes

For Cloudflare Pages, the `_redirects` file makes `/c/card001` route to `index.html`.

NFC cards should contain URLs like:

```text
https://your-domain.com/c/card001
```

