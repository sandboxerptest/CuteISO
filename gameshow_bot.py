#!/usr/bin/env python3
import socket
import ssl
import time
import random
import re

# --- BOT CONFIGURATION ---
SERVER = "irc.irchighway.net"
PORT = 6697
USE_SSL = True
NICK = "GameMaster"
CHANNEL = "#cuteiso" # Change this to the channel you want to play in

class GameBot:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if USE_SSL:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.sock = context.wrap_socket(self.sock, server_hostname=SERVER)
        
        self.active_game = None
        
        # Game States
        self.oak_target = 0
        self.boss_hp = 0
        self.cornhole_scores = {}

    def connect(self):
        print(f"Connecting to {SERVER}:{PORT}...")
        self.sock.connect((SERVER, PORT))
        self.send(f"NICK {NICK}")
        self.send(f"USER {NICK} 0 * :IRC GameShow Bot")
        
        while True:
            response = self.sock.recv(2048).decode("utf-8", errors="replace")
            for line in response.split("\r\n"):
                if not line: continue
                print(f"<< {line}")
                
                if line.startswith("PING"):
                    self.send("PONG" + line[4:])
                
                # Join channel on successful connect
                if " 001 " in line or " 266 " in line:
                    self.send(f"JOIN {CHANNEL}")
                    self.send_msg(CHANNEL, "👋 GameMaster bot online! Type !games to see what we can play.")

                # Handle messages
                if "PRIVMSG" in line:
                    self.handle_message(line)

    def send(self, msg):
        self.sock.send(f"{msg}\r\n".encode("utf-8"))

    def send_msg(self, target, msg):
        self.send(f"PRIVMSG {target} :{msg}")
        time.sleep(0.5) # Flood protection

    def handle_message(self, line):
        # Extract nick and message
        match = re.search(r":([^!]+)!.*? PRIVMSG ([^ ]+) :(.+)", line)
        if not match: return
        
        nick, target, text = match.groups()
        text = text.strip()
        cmd = text.lower().split(" ")[0]

        # --- MAIN MENU ---
        if cmd == "!games":
            self.send_msg(CHANNEL, "🎮 Available Games: 1. Oak Island Dig (!play oak) | 2. Mantra Weaver (!play mantra) | 3. Cornhole (!play cornhole)")
            if self.active_game:
                self.send_msg(CHANNEL, f"⚠️ Currently playing: {self.active_game}. Type !stop to end it.")

        elif cmd == "!stop" and self.active_game:
            self.send_msg(CHANNEL, f"⏹️ {self.active_game} stopped by {nick}.")
            self.active_game = None

        # --- GAME 1: OAK ISLAND DIG ---
        elif cmd == "!play" and "oak" in text.lower():
            if self.active_game: return self.send_msg(CHANNEL, "A game is already running!")
            self.active_game = "Oak Island Dig"
            self.oak_target = random.randint(10, 150)
            self.send_msg(CHANNEL, "🏴‍☠️ **OAK ISLAND DIG** starting! A Templar artifact is buried in the Money Pit.")
            self.send_msg(CHANNEL, "Type '!dig <number>' to dig between 10 and 150 feet. First to hit the exact depth wins!")

        elif cmd == "!dig" and self.active_game == "Oak Island Dig":
            try:
                depth = int(text.split(" ")[1])
                if depth < self.oak_target:
                    self.send_msg(CHANNEL, f"⛏️ {nick} dug to {depth}ft... You hit wood, but the artifact is DEEPER!")
                elif depth > self.oak_target:
                    self.send_msg(CHANNEL, f"⛏️ {nick} dug to {depth}ft... You flooded the shaft! The artifact is SHALLOWER!")
                else:
                    self.send_msg(CHANNEL, f"🎉🏆 WE HAVE A WINNER! {nick} found the Templar artifact at exactly {depth}ft!")
                    self.active_game = None
            except:
                pass

        # --- GAME 2: MANTRA WEAVER ---
        elif cmd == "!play" and "mantra" in text.lower():
            if self.active_game: return self.send_msg(CHANNEL, "A game is already running!")
            self.active_game = "Mantra Weaver"
            self.boss_hp = 150
            self.send_msg(CHANNEL, "🧙‍♂️ **MANTRA WEAVER** starting! A Dark Behemoth appears with 150 HP!")
            self.send_msg(CHANNEL, "Type '!cast <word>' to attack. Build words using prefixes (IGNO, AQUA, TOU) and suffixes (TE, NA, TES). e.g., '!cast IGNOTES'")

        elif cmd == "!cast" and self.active_game == "Mantra Weaver":
            spell = text[6:].upper().strip()
            if not spell: return
            
            damage = 0
            effect = "A fizzle of sparks."
            
            # Word-based magic logic
            if "IGNO" in spell: damage += 25; effect = "A burst of searing flames!"
            if "AQUA" in spell: damage += 20; effect = "A crushing wave of water!"
            if "TOU" in spell:  damage += 15; effect = "A violent gust of wind!"
            
            if "TES" in spell: damage = int(damage * 1.5); effect += " (Amplified!)"
            elif "TE" in spell: damage += 10
            if "NA" in spell: damage += 5; effect += " (Area damage!)"

            if damage == 0: damage = random.randint(1, 5); effect = "A weak, unstructured hex."

            self.boss_hp -= damage
            if self.boss_hp <= 0:
                self.send_msg(CHANNEL, f"✨ {nick} cast {spell}! {effect} It deals {damage} damage.")
                self.send_msg(CHANNEL, f"☠️ The Dark Behemoth has been vanquished by {nick}! YOU WIN!")
                self.active_game = None
            else:
                self.send_msg(CHANNEL, f"✨ {nick} cast {spell}! {effect} It deals {damage} damage. (Boss HP: {self.boss_hp}/150)")

        # --- GAME 3: CORNHOLE ---
        elif cmd == "!play" and "cornhole" in text.lower():
            if self.active_game: return self.send_msg(CHANNEL, "A game is already running!")
            self.active_game = "Cornhole"
            self.cornhole_scores = {}
            self.send_msg(CHANNEL, "🌽 **CORNHOLE CHAMPIONSHIP** starting! First to 5 points wins.")
            self.send_msg(CHANNEL, "Type '!toss' to throw your bag.")

        elif cmd == "!toss" and self.active_game == "Cornhole":
            roll = random.randint(1, 100)
            pts = 0
            if roll > 80:
                pts = 3
                result = "CORNHOLE! Nothin' but net! (3 pts)"
            elif roll > 40:
                pts = 1
                result = "Woody! It's on the board. (1 pt)"
            else:
                result = "Airmail... completely missed the board. (0 pts)"

            self.cornhole_scores[nick] = self.cornhole_scores.get(nick, 0) + pts
            score = self.cornhole_scores[nick]
            
            self.send_msg(CHANNEL, f"🎒 {nick} tosses... {result} | Total Score: {score}")
            
            if score >= 5:
                self.send_msg(CHANNEL, f"🏆 {nick} WINS THE CORNHOLE MATCH with {score} points!")
                self.active_game = None

if __name__ == "__main__":
    bot = GameBot()
    try:
        bot.connect()
    except KeyboardInterrupt:
        print("\nShutting down bot.")