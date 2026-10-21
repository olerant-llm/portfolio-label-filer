import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import webview
from api import Api
from model_manager import ensure_model


def main() -> None:
    print("Preparing the Label Filer image recognition model...")
    ensure_model()
    api = Api()
    # クエリ文字列を毎回変えることで、ブラウザ側の古いキャッシュが使われるのを防ぐ
    url = f"../static/index.html?v={int(time.time())}"
    window = webview.create_window(
        "Label Filer", url, js_api=api, width=1280, height=860
    )
    # ウィンドウの読み込みが完全に終わる前に参照を渡すと、Windows側で
    # アクセシビリティ情報の取得が無限にループするエラーが出ることがあるため、
    # 読み込み完了後（loadedイベント）に渡すようにする。
    window.events.loaded += lambda: setattr(api, "window", window)
    webview.start(http_server=True)


if __name__ == "__main__":
    main()
