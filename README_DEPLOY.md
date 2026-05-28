# MP Band Quiz v15 deploy guide

この版は frontend を build して FastAPI が同じ URL で配信します。Render/Fly.io/VPS/Docker で公開できます。

## 推奨: Render + GitHub

1. GitHub にこの `mp_band_quiz_real_v15` フォルダの中身を push します。
2. Render で New Web Service を作り、その GitHub repository を選びます。
3. Runtime は Docker を選びます。`Dockerfile` が自動利用されます。
4. Environment Variables に次を追加します。

```text
MP_API_KEY = あなたの Materials Project API key
```

5. Deploy します。以後は GitHub に push すれば自動更新できます。

`render.yaml` も入れてあるので、Blueprint として使うこともできます。

## Fly.io

```bash
fly launch
fly secrets set MP_API_KEY='your_api_key'
fly deploy
```

Dockerfile から deploy できます。

## ローカルで Docker 起動

```bash
docker build -t mp-band-quiz .
docker run --rm -p 8000:8000 -e MP_API_KEY='your_api_key' mp-band-quiz
```

ブラウザ: http://127.0.0.1:8000/

## 一時公開だけなら Cloudflare Quick Tunnel

```bash
export MP_API_KEY='your_api_key'
PORT=8010 ./run_public_cloudflare.sh
```

これはターミナルを閉じると止まります。常時公開は Render/Fly.io 等へ deploy してください。

## v15 の設計メモ

- v8 系の Plotly 可動 UI を維持。
- 正解時に簡易 confetti effect。
- デフォルトエネルギー範囲は `-12 eV` から `+12 eV`。
- デフォルトランダム方式は `balanced_live`。
- `balanced_live` は mp-id 乱数とランダム元素検索を毎回混ぜ、候補を重複除去してシャッフルします。
- 「候補を再検索」ボタンは廃止しました。
