import eventlet
eventlet.monkey_patch()

from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit
import random
from collections import Counter
import time

app = Flask(__name__, static_folder='.', static_url_path='')
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- Card and Deck Classes ---
SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
RANK_VALUES = {rank: i for i, rank in enumerate(RANKS, 2)}
HAND_NAMES = {
    10: "Royal Flush", 9: "Straight Flush", 8: "Four of a Kind", 7: "Full House",
    6: "Flush", 5: "Straight", 4: "Three of a Kind", 3: "Two Pair",
    2: "One Pair", 1: "High Card"
}

class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank
        self.value = RANK_VALUES[rank]

    def to_dict(self):
        return {"suit": self.suit, "rank": self.rank, "color": 'red' if self.suit in ['♥', '♦'] else 'black'}

class Deck:
    def __init__(self):
        self.cards = [Card(s, r) for s in SUITS for r in RANKS]
        self.shuffle()

    def shuffle(self):
        random.shuffle(self.cards)

    def deal(self):
        return self.cards.pop() if self.cards else None

# --- Hand Evaluation Logic ---
def evaluate_hand(hand):
    from itertools import combinations
    all_combinations = combinations(hand, 5)
    best_rank, best_hand_cards = -1, []
    for combo in all_combinations:
        rank_val, hand_cards = _calculate_hand_rank(list(combo))
        if rank_val > best_rank:
            best_rank, best_hand_cards = rank_val, hand_cards
        elif rank_val == best_rank and _compare_hands(hand_cards, best_hand_cards) > 0:
            best_hand_cards = hand_cards
    return (best_rank, best_hand_cards, HAND_NAMES.get(best_rank, "Unknown"))

def _calculate_hand_rank(hand):
    hand.sort(key=lambda card: card.value, reverse=True)
    values = [c.value for c in hand]
    ranks = [c.rank for c in hand]
    suits = [c.suit for c in hand]
    is_flush = len(set(suits)) == 1
    is_straight = all(values[i] - 1 == values[i+1] for i in range(4)) or (set(ranks) == {'A', '2', '3', '4', '5'})
    if is_straight and set(ranks) == {'A', '2', '3', '4', '5'}:
        hand = sorted(hand, key=lambda c: (c.value != 14, c.value))
    if is_straight and is_flush: return (10 if values[0] == 14 else 9, hand)
    counts = Counter(ranks)
    counts_vals = sorted(counts.values(), reverse=True)
    if counts_vals[0] == 4:
        four_rank = [r for r, c in counts.items() if c == 4][0]
        return (8, sorted(hand, key=lambda c: (c.rank != four_rank, c.value), reverse=True))
    if counts_vals == [3, 2]: return (7, hand)
    if is_flush: return (6, hand)
    if is_straight: return (5, hand)
    if counts_vals[0] == 3:
        three_rank = [r for r, c in counts.items() if c == 3][0]
        return (4, sorted(hand, key=lambda c: (c.rank != three_rank, c.value), reverse=True))
    if counts_vals == [2, 2, 1]:
        pair_ranks = [r for r, c in counts.items() if c == 2]
        return (3, sorted(hand, key=lambda c: (c.rank not in pair_ranks, c.value), reverse=True))
    if counts_vals[0] == 2:
        pair_rank = [r for r, c in counts.items() if c == 2][0]
        return (2, sorted(hand, key=lambda c: (c.rank != pair_rank, c.value), reverse=True))
    return (1, hand)

def _compare_hands(hand1, hand2):
    for c1, c2 in zip(hand1, hand2):
        if c1.value > c2.value: return 1
        if c1.value < c2.value: return -1
    return 0

# --- Player and Game Classes ---
class Player:
    def __init__(self, sid, name, chips, is_bot=False):
        self.sid, self.name, self.chips, self.is_bot = sid, name, chips, is_bot
        self.reset_for_hand()

    def reset_for_hand(self):
        self.hand, self.bet, self.in_hand, self.is_all_in, self.status = [], 0, True, False, ''

    def to_dict(self, show_cards=False):
        return { "name": self.name, "chips": self.chips, "isBot": self.is_bot, "hand": [c.to_dict() for c in self.hand] if show_cards or not self.is_bot else [{}, {}], "bet": self.bet, "inHand": self.in_hand, "status": self.status }

class Game:
    def __init__(self):
        self.players, self.deck, self.community_cards, self.pot, self.dealer_pos, self.current_player_index, self.current_bet, self.last_raise, self.stage, self.small_blind, self.big_blind, self.is_running = [], None, [], 0, -1, 0, 0, 0, "Idle", 10, 20, False

    def get_state(self, show_all_cards=False):
        return { "players": [p.to_dict(show_all_cards or not p.in_hand) for p in self.players], "communityCards": [c.to_dict() for c in self.community_cards], "pot": self.pot, "dealerPos": self.dealer_pos, "currentPlayerIndex": self.current_player_index, "stage": self.stage }

    def add_player(self, sid, name, chips, is_bot=False):
        if len(self.players) < 4: self.players.append(Player(sid, name, chips, is_bot))

    def start_game(self):
        if self.players: self.dealer_pos = random.randint(0, len(self.players) - 1)
        socketio.start_background_task(self.run)

    def run(self):
        self.is_running = True
        while self.is_running:
            self.play_hand()
            self.log_and_emit("--- Next hand in 5 seconds ---")
            for i in range(5, 0, -1):
                socketio.emit('timer_update', {'countdown': i}); socketio.sleep(1)
            socketio.emit('timer_update', {'countdown': 0})
            if len([p for p in self.players if p.chips > 0]) < 2:
                self.is_running = False; self.log_and_emit("Game Over! Please refresh to start a new game.")

    def play_hand(self):
        self.players = [p for p in self.players if p.chips > 0]
        if len(self.players) < 2: return
        [p.reset_for_hand() for p in self.players]
        self.deck, self.community_cards, self.pot, self.current_bet = Deck(), [], 0, 0
        self.dealer_pos = (self.dealer_pos + 1) % len(self.players)
        self.log_and_emit("--- New Hand Starting ---"); socketio.sleep(1)
        self.post_blinds(); self.deal_hole_cards()
        
        if self.betting_round('Pre-Flop'):
            if self.betting_round('Flop', 3):
                if self.betting_round('Turn', 1):
                    if self.betting_round('River', 1):
                        self.showdown()
                    else: self.award_pot_to_winner()
                else: self.award_pot_to_winner()
        else: self.award_pot_to_winner()

    def award_pot_to_winner(self):
        contenders = [p for p in self.players if p.in_hand]
        if len(contenders) == 1:
            winner = contenders[0]
            self.log_and_emit(f"{winner.name} wins the pot of {self.pot}!")
            winner.chips += self.pot
            self.pot = 0
        self.emit_game_state()

    def betting_round(self, stage_name, cards_to_deal=0):
        self.stage = stage_name
        self.log_and_emit(f"--- {stage_name} ---")

        if stage_name != 'Pre-Flop':
            self.current_bet = 0
            self.last_raise = self.big_blind
            for p in self.players: p.bet = 0
            self.current_player_index = self.get_next_active_player(self.dealer_pos)
        
        if cards_to_deal > 0:
            self.deck.deal()
            [self.community_cards.append(self.deck.deal()) for _ in range(cards_to_deal)]

        players_with_option = len([p for p in self.players if p.in_hand and not p.is_all_in])
        if players_with_option < 2 and self.current_bet == 0:
            self.collect_bets()
            return len([p for p in self.players if p.in_hand]) > 1

        action_starter_index = self.current_player_index
        
        while True:
            players_in_hand = [p for p in self.players if p.in_hand]
            if len(players_in_hand) <= 1:
                break
                
            player = self.players[self.current_player_index]

            if player.is_all_in or not player.in_hand:
                self.current_player_index = self.get_next_active_player(self.current_player_index)
                if self.current_player_index == action_starter_index:
                    break
                else:
                    continue

            self.log_and_emit(f"Turn for {player.name} (SID: {player.sid}).")
            self.emit_game_state()
            to_call = self.current_bet - player.bet
            
            action = self.get_bot_action(player, to_call, self.stage) if player.is_bot else self.wait_for_player_action(player, to_call)
            action_result = self.handle_player_action(self.current_player_index, action)
            
            if action_result.get('is_raise'):
                action_starter_index = self.current_player_index
            
            self.current_player_index = self.get_next_active_player(self.current_player_index)
            
            if self.current_player_index == action_starter_index:
                highest_bet = max(p.bet for p in players_in_hand)
                if all(p.bet == highest_bet or not p.in_hand or p.is_all_in for p in self.players):
                    break

        self.collect_bets()
        return len([p for p in self.players if p.in_hand]) > 1

    def wait_for_player_action(self, player, to_call):
        socketio.emit('your_turn', {'toCall': to_call, 'minRaise': self.last_raise, 'chips': player.chips}, to=player.sid)
        player.event = eventlet.event.Event()
        return player.event.wait()

    def handle_player_action(self, p_idx, data):
        player, action, amount, to_call = self.players[p_idx], data.get('action'), int(data.get('amount', 0)), self.current_bet - self.players[p_idx].bet
        is_raise = False
        if action == 'fold': player.in_hand, player.status = False, 'FOLD'; self.log_and_emit(f"{player.name} folds.")
        elif action == 'check': self.log_and_emit(f"{player.name} checks.")
        elif action == 'call':
            call_amount = min(to_call, player.chips); player.chips -= call_amount; player.bet += call_amount
            self.log_and_emit(f"{player.name} calls {call_amount}.")
        elif action in ['bet', 'raise']:
            total_bet = min(amount, player.chips + player.bet); additional = total_bet - player.bet
            player.chips -= additional; player.bet += additional
            self.log_and_emit(f"{player.name} {'bets' if to_call == 0 else 'raises'} to {player.bet}.")
            if player.bet > self.current_bet:
                self.last_raise, self.current_bet, is_raise = player.bet - self.current_bet, player.bet, True
        if player.chips == 0: player.is_all_in, player.status = True, 'ALL-IN'
        self.emit_game_state(); socketio.sleep(1)
        return {'is_raise': is_raise}

    def collect_bets(self):
        for p in self.players: self.pot += p.bet; p.bet = 0

    def showdown(self):
        self.log_and_emit('--- Showdown ---'); self.emit_game_state(show_all_cards=True)
        contenders = [p for p in self.players if p.in_hand]
        if not contenders or len(contenders) < 2:
            self.award_pot_to_winner()
            return
            
        winners, best_rank = [], -1
        for p in contenders:
            p.hand_result = evaluate_hand(p.hand + self.community_cards); rank, cards, name = p.hand_result
            self.log_and_emit(f"{p.name} has: {name}")
            if rank > best_rank: best_rank, winners = rank, [p]
            elif rank == best_rank:
                if _compare_hands(cards, winners[0].hand_result[1]) == 1: winners = [p]
                elif _compare_hands(cards, winners[0].hand_result[1]) == 0: winners.append(p)
        
        winnings = self.pot // len(winners)
        for w in winners: w.chips += winnings; self.log_and_emit(f"{w.name} wins {winnings} with {w.hand_result[2]}")
        self.pot = 0; self.emit_game_state()

    def get_next_active_player(self, start_index):
        i = start_index
        for _ in range(len(self.players) + 1):
            i = (i + 1) % len(self.players)
            if self.players[i].in_hand and not self.players[i].is_all_in: return i
        return start_index
    
    def post_blinds(self):
        sb_pos, bb_pos = self.get_next_active_player(self.dealer_pos), self.get_next_active_player(self.get_next_active_player(self.dealer_pos))
        sb_amount = min(self.small_blind, self.players[sb_pos].chips); self.players[sb_pos].chips -= sb_amount; self.players[sb_pos].bet = sb_amount
        self.log_and_emit(f"{self.players[sb_pos].name} posts small blind of {sb_amount}")
        bb_amount = min(self.big_blind, self.players[bb_pos].chips); self.players[bb_pos].chips -= bb_amount; self.players[bb_pos].bet = bb_amount
        self.log_and_emit(f"{self.players[bb_pos].name} posts big blind of {bb_amount}")
        self.current_bet, self.last_raise, self.current_player_index = self.big_blind, self.big_blind, self.get_next_active_player(bb_pos)

    def deal_hole_cards(self):
        for _ in range(2):
            for p in self.players:
                if p.in_hand: p.hand.append(self.deck.deal())
    
    def emit_game_state(self, show_all_cards=False): socketio.emit('game_state_update', self.get_state(show_all_cards))
    def log_and_emit(self, message): print(message); socketio.emit('log_message', message)

    def get_bot_action(self, player, to_call, stage):
        socketio.sleep(1.5)
        hole_vals, hole_ranks, is_pair = sorted([c.value for c in player.hand], reverse=True), sorted([c.rank for c in player.hand], reverse=True), len(set(c.rank for c in player.hand)) == 1
        if stage == 'Pre-Flop':
            action = 'check' if to_call == 0 else 'call'
            if (is_pair and hole_vals[0] >= 11) or set(hole_ranks) == {'A', 'K'}:
                raise_amt = self.current_bet + max(self.last_raise, self.big_blind) * 2
                return {'action': 'raise', 'amount': min(player.chips + player.bet, raise_amt)}
            if (is_pair and hole_vals[0] >= 8) or set(hole_ranks) == {'A', 'Q'}: return {'action': action}
            if is_pair and to_call > 0 and to_call < player.chips * 0.1: return {'action': 'call'}
            return {'action': 'fold'} if to_call > 0 else {'action': 'check'}
        else:
            strength = evaluate_hand(player.hand + self.community_cards)[0]
            if to_call == 0: return {'action': 'bet', 'amount': max(self.big_blind, min(player.chips, int(self.pot*0.75)))} if strength >= 4 else {'action': 'check'}
            else:
                if to_call > player.chips * 0.3: return {'action': 'fold'}
                if strength >= 5: raise_amt = self.current_bet + max(self.last_raise, to_call) * 2; return {'action': 'raise', 'amount': min(player.chips + player.bet, raise_amt)}
                if strength >= 3: return {'action': 'call'}
                return {'action': 'fold'}

game = Game()

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f'Client connected: {sid}')
    human_player = next((p for p in game.players if not p.is_bot), None)

    if not human_player:
        game.add_player(sid, 'You', 1000)
        game.add_player(None, 'Bot Alice', 1000, is_bot=True)
        game.add_player(None, 'Bot Bob', 1000, is_bot=True)
        game.add_player(None, 'Bot Charlie', 1000, is_bot=True)
        if not game.is_running:
            game.start_game()
    else:
        human_player.sid = sid
        print(f"Re-assigned sid {sid} to {human_player.name}")
        if game.players and game.players[game.current_player_index] == human_player:
            player = game.players[game.current_player_index]
            to_call = game.current_bet - player.bet
            print(f"Re-emitting 'your_turn' to {player.name} on reconnect.")
            socketio.emit('your_turn', {'toCall': to_call, 'minRaise': game.last_raise, 'chips': player.chips}, to=sid)
    
    game.emit_game_state()


@socketio.on('player_action')
def on_player_action(data):
    p = next((p for p in game.players if not p.is_bot and hasattr(p, 'event') and p.event and not p.event.ready()), None)
    if p: p.event.send(data)

if __name__ == '__main__':
    print("Server starting on http://127.0.0.1:5001")
    socketio.run(app, host='0.0.0.0', port=5001)

