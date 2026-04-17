import sys
import os
import time
import logging
import httpx

# ルートディレクトリをパスに追加
root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(root_path)

from state_manager import StateManager
from ai import AICompanion

# ログレベル調整
logging.basicConfig(level=logging.INFO)

def test_phase3_logic():
    print("=== Phase 3: Logic Smoke Test ===")
    
    sm = StateManager()
    # 初期化時にモデルが見つからなくてもテストを続けられるようにモック
    ai = AICompanion()

    # 1. 操作強度のテスト
    print("\n[1/4] Testing Input Intensity...")
    # 疑似的に激しい連打をシミュレート (30回)
    for _ in range(30):
        sm.record_input_event()
    
    # tickを実行して強度を計算 (EMA更新)
    sm.tick()
    state = sm.get_state()
    print(f"Current Intensity: {state.input_intensity:.2f} ({state.input_label})")
    
    if state.input_intensity > 0.0:
        print("[OK] Input monitoring integration is working.")
    else:
        print("[NG] Intensity is still 0. Check record_input_event.")

    # 2. VLM 履歴のテスト
    print("\n[2/4] Testing Visual History (Transition)...")
    ai.set_vision_context("敵が正面に見える")
    ai.set_vision_context("敵を倒して、アイテムがドロップした")
    
    # 内部の _vision_history を確認
    history = ai._vision_history
    history_line = " -> ".join([f"[{i+1}]{ctx}" for i, ctx in enumerate(history)])
    print(f"Visual History Prompt: {history_line}")
    
    if len(history) >= 2:
        print("[OK] Visual history (Transition) is working.")
    else:
        print("[NG] Visual history not updated.")

    # 3. 依存関係の接続確認 (Ollama, VOICEVOX)
    print("\n[3/4] Checking Dependencies...")
    
    # Ollama
    try:
        res = httpx.get("http://localhost:11434/api/tags", timeout=1.0)
        if res.status_code == 200:
            print("[OK] Ollama is running.")
        else:
            print("[WARN] Ollama responded but with error status.")
    except Exception:
        print("[NG] Ollama is NOT running at localhost:11434.")

    # VOICEVOX
    try:
        res = httpx.get("http://localhost:50021/version", timeout=1.0)
        if res.status_code == 200:
            print(f"[OK] VOICEVOX is running (version: {res.text}).")
        else:
            print("[WARN] VOICEVOX responded but with error status.")
    except Exception:
        print("[NG] VOICEVOX is NOT running at localhost:50021.")

    # 4. プロンプト注入テスト
    print("\n[4/4] Testing Prompt Injection...")
    print("Player State Summary:")
    summary = state.summary()
    print(summary)
    
    if "操作強度" in summary:
        print("[OK] State summary includes input intensity.")

    print("\n=== Smoke Test Completed ===")

if __name__ == "__main__":
    test_phase3_logic()
