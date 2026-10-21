import re
import os
from pathlib import Path
from datetime import datetime
from janome.tokenizer import Tokenizer
from pypdf import PdfReader

# グローバルな Janome トークナイザ
_tokenizer = Tokenizer()

# 除外するストップワード（不適切な一般名詞など）
STOP_WORDS = {
    "こと", "もの", "ため", "これ", "それ", "あれ", "よう", "そう", 
    "の", "ん", "ところ", "うち", "わけ", "はず", "つもり", "せい", 
    "とき", "じ", "し", "き", "み", "り", "お", "ご", "等", "など", 
    "よう", "そう", "ほう", "側", "中", "内", "外", "間", "上", "下", 
    "前", "後", "中", "件", "目", "度", "回", "年", "月", "日", 
    "平成", "令和", "昭和", "西暦", "ファイル", "データ", "テキスト",
    "サンプル", "テスト", "一覧", "情報", "内容", "作成", "更新", "実行",
    "確認", "処理", "結果", "状態", "機能", "システム", "設定", "表示",
    "決定", "変更", "追加", "削除", "調整", "報告", "連絡", "相談"
}

def extract_text(path: Path) -> str:
    """ファイルからプレーンテキストを抽出する"""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".log"):
        try:
            # UTF-8 で試みるが、失敗時は cp932 (Shift_JIS) でフォールバック
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return path.read_text(encoding="cp932", errors="ignore")
        except Exception as e:
            print(f"Error reading text file {path}: {e}")
            return ""
    elif suffix == ".pdf":
        try:
            reader = PdfReader(path)
            text_parts = []
            # ページ数が非常に多い場合に備えて、最大10ページ程度に制限して動作を軽くする
            max_pages = min(10, len(reader.pages))
            for i in range(max_pages):
                page_text = reader.pages[i].extract_text()
                if page_text:
                    text_parts.append(page_text)
            return "\n".join(text_parts)
        except Exception as e:
            print(f"Error reading PDF file {path}: {e}")
            return ""
    return ""

def get_rule_based_tags(path: Path) -> list[tuple[str, float]]:
    """ファイル名やメタデータからルールベースのタグを抽出する (スコアは1.0固定)"""
    tags = []
    
    # 1. 拡張子タグ
    ext = path.suffix.lower().lstrip(".")
    if ext:
        tags.append((ext, 1.0))
        
    # 2. ファイル更新年度タグ
    try:
        stat = path.stat()
        # ローカルタイムで年度を取得
        year = datetime.fromtimestamp(stat.st_mtime).year
        tags.append((f"{year}年", 1.0))
        # 4桁の数値タグ（検索用）
        tags.append((str(year), 1.0))
    except Exception:
        pass

    # 3. ファイル名に含まれる日付（表記揺れ対応：20260629, 2026-06-29, 2026_06_29）
    filename = path.name
    # 8桁日付のパターン (例: 20260629, 2026-06-29, 2026_06_29)
    date_match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", filename)
    if date_match:
        year_str = date_match.group(1)
        month_str = date_match.group(2)
        day_str = date_match.group(3)
        
        # 月日の妥当性チェック（簡単な範囲チェック）
        try:
            m = int(month_str)
            d = int(day_str)
            if 1 <= m <= 12 and 1 <= d <= 31:
                # 統一形式の日付タグ (例: 2026-06-29)
                unified_date = f"{year_str}-{month_str}-{day_str}"
                tags.append((unified_date, 1.0))
                # 年度タグ（もし年が上で取得したものと違ってもファイル名優先で追加）
                tags.append((year_str, 1.0))
                tags.append((f"{year_str}年", 1.0))
        except ValueError:
            pass
            
    # 4. ファイル名に含まれるその他の年度パターン（西暦4桁、和暦など）
    # 日付にヒットしなかった場合、独立した「2024」〜「2030」などの4桁西暦を検索
    if not date_match:
        year_match = re.search(r"\b(20[2-3]\d)\b", filename)
        if year_match:
            tags.append((year_match.group(1), 1.0))
            tags.append((f"{year_match.group(1)}年", 1.0))
            
    # 和暦パターン（R06, H30, 令和6, 平成30 など）
    wareki_match = re.search(r"(?:^|[^a-zA-Z0-9])([RH]\d{1,2})(?:$|[^a-zA-Z0-9])|((?:令和|平成)\d{1,2})", filename)
    if wareki_match:
        w_tag = wareki_match.group(1) or wareki_match.group(2)
        tags.append((w_tag, 1.0))
        
    return tags

def get_nlp_tags(text: str, limit: int = 15) -> list[tuple[str, float]]:
    """Janome形態素解析を使って文章中から重要キーワードを抽出する"""
    if not text.strip():
        return []
        
    word_counts = {}
    try:
        tokens = _tokenizer.tokenize(text)
        for token in tokens:
            pos = token.part_of_speech
            # 「名詞」かつ「数」「非自立」「代名詞」「接尾」以外の主要名詞を対象にする
            if pos.startswith("名詞"):
                # サブ分類のフィルタリング
                sub_pos = pos.split(",")[1]
                if sub_pos in ("一般", "固有名詞", "サ変接続"):
                    word = token.surface.strip()
                    # 1文字の名詞はノイズが多いため除外
                    if len(word) <= 1:
                        continue
                    # ストップワードの除外
                    if word in STOP_WORDS:
                        continue
                    # 数字だけの単語は除外（年度等はルールベースで処理するため）
                    if word.isdigit():
                        continue
                    word_counts[word] = word_counts.get(word, 0) + 1
    except Exception as e:
        print(f"Error during morphology analysis: {e}")
        return []
        
    # 出現回数順にソート
    sorted_words = sorted(word_counts.items(), key=lambda x: -x[1])
    
    # 上位 limit 件をスコア 1.0 で返す（タグとしての有無が重要なため）
    return [(word, 1.0) for word, count in sorted_words[:limit]]

def tag_document(path: Path) -> list[tuple[str, float]]:
    """ドキュメントファイルからタグを抽出し、一括で返すメイン関数"""
    if not path.exists():
        return []
        
    # 1. ルールベースのタグ（確定タグ）
    rule_tags = get_rule_based_tags(path)
    
    # 重複排除用のセット
    seen = {t[0] for t in rule_tags}
    
    # 2. テキスト抽出 ＆ 形態素解析によるタグ
    text = extract_text(path)
    nlp_tags = get_nlp_tags(text, limit=15)
    
    # 結果の統合
    result = list(rule_tags)
    for word, score in nlp_tags:
        if word not in seen:
            result.append((word, score))
            seen.add(word)
            
    return result
