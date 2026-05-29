# Deploy guide: MP Band Quiz v16 Speed

## Render に更新する場合

1. 既存の GitHub repository の中身をこの v16 Speed の中身で置き換えます。
2. commit/push します。
3. Render の Auto Deploy が ON なら自動で再ビルドされます。

```bash
cd path/to/mp-band-quiz
# 必要なら既存ファイルをバックアップしてから v16 の中身をコピー
git add .
git commit -m "Update to v16 speed random mode"
git push
```

## Render の環境変数

Render の Environment Variables に必ず設定します。

```text
MP_API_KEY = あなたの Materials Project API key
```

## Dockerfile

Render では Dockerfile を使う想定です。Build Command / Start Command は空欄で構いません。Dockerfile の CMD が Render の `$PORT` に bind します。

## 速度について

この版は full quiz cache をデフォルトでは使いません。代わりに、候補 mp-id だけをメモリに保持します。

- 同じ問題ばかり出るリスク: full cache より低い
- 速度: 完全ライブ検索より速い
- 初回: MP API summary search のため遅いことがあります
- 2問目以降: service が起動している限り候補IDプールにより速くなります

## Render Free の注意

Render Free では sleep/cold start があります。また、file cache は永続化しません。常時高速化したい場合は、Render 有料インスタンス、Redis/Postgres、または外部DBによる候補ID index 化が必要です。
