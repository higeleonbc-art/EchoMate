import sys
import os
sys.path.insert(0, r"d:\github\EchoMate")
os.chdir(r"d:\github\EchoMate")

from ai import AICompanion
from state_manager import StateManager

ai = AICompanion()
ai.set_character('echo')
memory = {'recent_topics': ['LoL', 'エズリアル']}
state = StateManager().get_state()

pairs = [
    'エズリアルって攻撃が難しいって聞くけど',
    'あるけど、攻撃は別に難しくないよ',
    'めんどくない',
    'メインスキルが当たらんとダメージソースがないぐらいだね',
]

for p in pairs:
    r = ai.get_response(p, memory, state)
    print(f'P: {p}')
    print(f'AI: {r}')
    print()
