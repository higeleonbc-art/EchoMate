import sys, os
sys.path.insert(0, r"d:\github\EchoMate")
os.chdir(r"d:\github\EchoMate")

from ai import AICompanion
from state_manager import StateManager

ai = AICompanion()
ai.set_character('echo')
memory = {'recent_topics': ['LoL', 'エズリアル']}
state = StateManager().get_state()

# 手動で会話履歴を作って、3ターン目のプロンプトを確認する
ai._conversation_history = [
    {"player": "エズリアルって攻撃が難しいって聞くけど", "ai": "うん、まあそんな感じの話してるのか。最近のゲーム、結構見てるんだよ。でも話してないから、ちょっとよくわかんないな。"},
    {"player": "あるけど、攻撃は別に難しくないよ", "ai": "うん、じゃあそっちの方がマシなんだろうな。でも、攻撃が難しいって聞くけど、実際はそんなにめんどくないのかな？"},
]

# プロンプトを直接確認（_call_with_validationを呼ばずにプロンプト構造だけチェック）
history_ctx = ai._build_history_context()
memory_ctx = ai._build_memory_context(memory)
player_input = "めんどくない"

print("=== 会話履歴コンテキスト ===")
print(repr(history_ctx))
print()

prompt = (
    f"{history_ctx}"
    f"プレイヤー: {player_input}\n"
    f"あなた: "
)
print("=== 生成プロンプト ===")
print(prompt)
print()
print("=== system_prompt ===")
print(ai.current_character.get("system_prompt", ""))
