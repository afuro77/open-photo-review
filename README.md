# Open Photo Review

Windowsで重複写真を見比べて整理する、日本語のデスクトップアプリです。写真の照合はPC上で行います。

## 起動

Windows 10または11と、Python 3.12（Tkinter付き）が必要です。展開したフォルダでPowerShellを開き、次を実行します。パスは自分の写真フォルダに置き換えてください。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scan.py 'D:\Photos'
.\.venv\Scripts\python.exe photo_review.py 'D:\Photos'
```

Google Driveの同期フォルダを開くときは、最後のコマンドに `--google-drive` を追加すると、ごみ箱へ移す前の確認画面に、ごみ箱へ移す前の確認画面に、クラウド側にも移動が反映される旨を表示します。

## 表示言語

右上の「設定」で日本語と英語を切り替えられます。保存すると同じグループを選んだ状態で画面が開き直ります。

## 比較と整理

- **完全に同じ写真**は、ファイルサイズで候補を絞り、SHA-256で照合します。
- **見た目が似ている写真**は、縮小画像の色や構図を比較します。似ている別の写真も候補に出るため、プレビューで確認してください。
- おすすめは容量が大きい方です。同じ容量なら、元の名前に見える方、XMPやAAEが付いている方を優先します。
- 初期設定では、ごみ箱へ移す前に確認します。画面右上の「設定」で、グループ整理と一括整理の確認の有無を個別に選べます。おすすめの優先順位も変更できます。一括ボタンは完全一致のグループに限定し、各グループに残す写真と編集情報付きの写真を保護します。
- スキャン結果と振り分けは、フォルダ単位で `%LOCALAPPDATA%\OpenPhotoReview` に保存します。写真を追加したら、アプリを閉じて再スキャンしてください。CSVには写真の相対パスが含まれます。

対応する形式の例はJPEG、PNG、HEIC、Olympus ORFです。プレビューできる形式は、画像ライブラリやファイルの状態に依存します。

## 一覧の操作

- スライダーでグループを移動できます。「次の未確認へ」は、元の場所に写真が2枚以上あり、「保留」が残っているグループを開きます。
- 「おすすめを残し、ほかをごみ箱へ」で移動が完了すると、次の未確認グループに進みます。自動移動は設定でオフにできます。
- ごみ箱から写真を元の場所に戻した場合は、「一覧を再読み込み」を押すと、復元した写真が「保留」に戻ります。

- 「作業ログ」で、このPCからごみ箱へ移した写真や復元を確認した写真を、日時と相対パス付きで見返せます。ログはこのPCに保存します。

## ライセンス

このプロジェクトのソースは [MIT License](LICENSE) で提供します。依存ライブラリにはそれぞれのライセンスが適用されます。

## English quick start

Open Photo Review compares exact duplicates and visually similar photos locally on Windows. Install the Python dependencies, run `python scan.py PHOTO_FOLDER`, then run `python photo_review.py PHOTO_FOLDER`. Open **Settings**, choose **English**, and save. Review choices, preferences, and activity logs are stored locally. Check your candidates before moving them to the Recycle Bin, especially in **Visually similar** mode.
