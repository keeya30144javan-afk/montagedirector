"""Every word list the reader knows, in one place.

To teach the reader a new phrase, this is usually the only file to change:
add the words to the matching list, then add the phrase to tests/phrasings.py.
"""

from __future__ import annotations

import re

SECTION_WORDS = {
    "intro": "intro", "beginning": "intro", "opening": "intro", "verse": "verse", "verses": "verse",
    "chorus": "drop", "choruses": "drop", "hook": "drop", "drop": "drop", "drops": "drop",
    "build": "build", "buildup": "build", "build-up": "build", "build up": "build", "pre-chorus": "build",
    "prechorus": "build", "breakdown": "breakdown", "bridge": "breakdown", "outro": "outro", "ending": "outro",
}
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
            "last": -1, "final": -1}
# Musical moments the app hears (most specific first). Section nouns are handled separately.
CUES = [
    ("hits", r"hard (?:drums|hits|stabs)|\b1\s*,?\s*2\s*,?\s*3\s*,?\s*4\b|\bstabs\b|\baccents\b|big hits|four hits"
             r"|the hits\b(?! in)"),
    ("solo", r"guitar solo|\bsolo\b|\blead (?:guitar|melody)\b"),
    ("drums_faster", r"drums?\b.{0,30}?\b(?:faster|frantic|speed|speeding|busier|intense|crazy)"
                     r"(?: and (?:more |even more )?(?:frantic|intense|crazy|busier|faster|wild))*|\bfrantic drums\b"
                     r"|double time drums"),
    ("ringout", r"note (?:ends|rings|fades)|rings? out|\bringing\b|\brings\b|last note|end of the song|\bthe end\b"
                r"|\bat the end\b|\bend it\b|\bend on\b|\bend with\b|\bthe very end\b"),
    ("predrop", r"\bbefore (?:the|each|every|a) (?:bass |beat |big )?drops?|\bleading (?:up )?(?:in)?to the drops?"),
    ("drop", r"bass (?:drops|comes in|hits|kicks in)|beat (?:drops|comes in|kicks in|hits)|\bkicks in\b"
             r"|\bthe drop hits\b|\bdrop comes in\b"),
    ("vocals", r"\b(?:vocals?|singing|singer|lyrics|voice|rapping|rap) (?:come|comes|start|starts|kick|kicks|begin|begins)"
               r"|\bwhen (?:he|she|they) (?:starts? )?(?:sings?|raps?)"),
    ("stop", r"\b(?:music )?(?:stops|goes quiet|cuts out|goes silent)\b|\bthe silence\b|\bthe pause\b"),
    ("fill", r"drum fills?|\bfills?\b"),
    ("intro", r"\bat the (?:start|beginning)\b|\bstart of the song\b|\bto start\b|\bstart off\b|cymbals|"
              r"\b(?:start|begin)(?:s|ing)? (?:it |the edit |off |out )?(?=slow|with|calm|chill|soft|quiet|by|on)"),
]
CUE_NAMES = {"hits": "hard hits", "solo": "solo", "drums_faster": "drums get faster", "ringout": "last note rings",
             "drop": "bass drops", "stop": "music stops", "fill": "drum fill", "intro": "intro",
             "predrop": "lead-in to the drop", "vocals": "vocals come in"}

STRONG = r"(?:super|really|very|so|extremely|insanely|ultra|mega|way|hella|absolutely|totally|max|maximum|too)"
MILD = r"(?:a little|a bit|a little bit|slightly|kinda|kind of|somewhat|a tad|bit|mildly|little)"
NONE = r"(?:no|not|without|don't|never|zero|hate|avoid|stop|skip|remove|get rid of|lose the|nothing|none|isn't|aren't|shouldn't|won't|can't)"
LESS = r"(?:less|fewer|not as many|not so many|not as much|tone down|toned down|lower|reduce|cut down on|not too many|dial back|dial down|calm down the|too many|too much)"
LIGHT = r"(?:a few|some|light|subtle|little|small|minimal|occasional|slight|soft|a couple|couple of|just a few|only a few|sprinkle of|bit of|a bit of|hint of)"
MORE = r"(?:more|lots of|lot of|a lot of|tons of|ton of|heavy|heavier|extra|plenty of|loads of|max|crazy|big|bigger|strong|stronger|many|frequent|constant|all the|insane|nonstop|so many|alot of)"

EFFECT_WORDS = [  # (pattern, effect)
    (r"white flash(?:es)?|flash(?:es|ing|y)?", "flash"),
    (r"zoom(?:s|ing)?(?: punch(?:es)?| ins?)?|punch[- ]?ins?", "zoom"),
    (r"(?:camera |screen )?shak(?:e|es|y|ing)|wobble", "shake"),
    (r"glitch(?:es|y|ing)?|rgb(?: split)?|chromatic(?: aberration)?|datamosh(?:ing)?", "rgb"),
    (r"slow motion", "slowmo"),
    (r"revers(?:e|ed|ing)|rewind(?:s|ing)?|backwards", "reverse"),
    (r"(?:black )?flicker(?:s|ing|y)?|black strobes?|strob(?:e|es|ing) (?:to|through) black|blackout flickers?|"
     r"blink(?:s|ing)? to black", "flicker"),
    (r"invert(?:ed|s)?|negative(?: flash(?:es)?)?|strobe(?:s|ing)?", "invert"),
    (r"stutter(?:s|ing)?(?: cuts?)?|repeat(?:ed|ing)? frames|skipping tape|tape skips?", "stutter"),
]
FAST = (r"fast(?:er)?|quick(?:er|ly)?|rapid(?:ly)?|hype(?:d)?|aggressive|crazy|insane|intense|chaotic|wild|"
        r"go(?:es)? off|go hard|goes hard|speed (?:it )?up|speeds up|pick(?:s)? (?:it |back )?up|picks back up|energetic|"
        r"high energy|frantic|flurry|barrage|hectic|nonstop|hard[- ]hitting|lit|snappy|choppy|rapid fire|"
        r"go (?:all )?out|turn (?:it )?up|amp(?:ed)? (?:it )?up|go nuts|go ham|explode|hit hard|hits hard|hit harder|hits harder|harder|"
        r"more cuts|cut more|more often|shorter cuts|more energy|energy|pumped|pump it up|bang|banger|turnt|lively")
SLOW = (r"slow(?:er)?(?! motion)|slow (?:it )?down|slows down|calm(?:er)?|chill(?:er)?|mellow|relax(?:ed|ing)?|gentle|"
        r"peaceful|soft(?:er)?|emotional|sad|melanchol(?:y|ic)|laid back|take it easy|long(?:er)? (?:shots|cuts|clips|scenes|takes)|play (?:out )?longer|(?:scenes?|shots?|clips?) (?:play|last|run) longer|"
        r"last longer|stay (?:on screen )?longer|hold (?:them |it |each (?:one |shot )?)?longer|"
        r"hold (?:the |each )?(?:shots?|clips?|scenes?) (?:a bit |a little )?longer|give (?:each|every|the) (?:shot|clip|scene)s? "
        r"more time|more time (?:on|for) (?:each|every)|"
        r"(?:not|don't|no) (?:use |have |do )?(?:so|too|as) many cuts|too many cuts|less cutting|cut less often|"
        r"let it breathe|breathe|tone it down|calm it down|ease up|low energy|quiet|mysterious|eerie|creepy|spooky|"
        r"ominous|haunting|less cuts|fewer cuts|cut less|less often|longer cuts|drawn[- ]out|play out|let (?:them|it|the "
        r"(?:fights?|shots?|scenes?)) play")
NORMAL = (r"back to (?:normal|the flow|the groove|regular|how it was|the melody|the (?:main )?(?:beat|vibe|rhythm|pace))|"
          r"normal speed|regular speed|medium speed|normal cuts|regular cuts|"
          r"normal pace|regular pace|the flow|groove|steady|medium pace|go back|goes back|melody")
HOLD = (r"still shot|one shot|single shot|\bhold(?:s|ing)?\b|held shot|panning shot|pan shot|one long shot|"
        r"stay on (?:one|a|the)|linger|let (?:it|the shot) play|don't cut|no cuts|long take|"
        r"\bplay(?:s|ing)? (?:it |this |that |the \w+ )?(?:out|through)\b|\blet (?:it|the \w+|this \w+) play\b|"
        r"\buncut\b|\bwithout (?:any )?cuts\b|\b(?:whole|full|entire|complete) (?:fight|battle|scene|sequence)\b|"
        r"\ba still\b|\bstill (?:of|frame|image|picture)\b|\b(?:final|last|closing|ending|end) shot\b|"
        r"\bend (?:it )?(?:on|with) (?:a|one) (?:shot|still|image|picture)\b|group shot|wide shot")
# "play out this battle scene": let the original footage run on through its own cuts (not one camera shot)
PLAY_OUT = (r"\bplay(?:s|ing)? (?:it |this |that |the \w+ )?(?:out|through)\b|\blet (?:it|the \w+|this \w+) play\b|"
            r"\buncut\b|\b(?:whole|full|entire|complete) (?:fight|battle|scene|sequence)\b|\bdon'?t cut away\b|"
            r"\b(?:fight|battle|scene|sequence) plays? out\b|\bas it (?:is|was) in the (?:show|anime|episode|video)\b")
# Places people ask for ("fighting in space"): also looked up in the shot descriptions
SETTINGS = ("space", "orbit", "stars", "galaxy", "planet", "moon", "city", "desert", "ocean", "sea", "underwater",
            "forest", "jungle", "snow", "rain", "night", "sky", "clouds", "ruins", "volcano", "cave", "mountains",
            "beach", "school", "street", "rooftop", "arena", "colony", "asteroid", "asteroids", "battleship")
RAMP = (r"faster and faster|quicker and quicker|more and more cuts|speed(?:s|ing)? up (?:gradually|more and more)|"
        r"gradually (?:faster|quicker|speed(?:s|ing)? up)|build(?:s|ing)? (?:up )?(?:the )?tension|accelerat\w+|"
        r"getting (?:faster|quicker)|ramp(?:s|ing)? up|cuts? speed(?:s|ing)? up|build(?:s|ing)? up (?:the )?(?:speed|pace|cuts)")
VELOCITY = r"velocity|speed ramps?|speed-ramp(?:s|ing)?|speed changes|time ?remap"
TRANSITION_WORDS = [  # (pattern, kind), most specific first
    (r"\b(?:quick|short|fast|little|tiny) (?:fades?|blends?|crossfades?|dissolves?)\b|\bsoft cuts\b|\bghost(?:y|ing)? "
     r"(?:cuts|blends)\b", "quick"),
    (r"\b(?:cross ?fades?|cross-fades?|crossfading|cross[- ]dissolves?|fades? between|blend(?:s|ing)? (?:between|into)|"
     r"fade (?:from one|between) (?:shot|scene|clip)s?)\b", "crossfade"),
    (r"\bdissolves?\b", "dissolve"),
    (r"\bwhip(?:s| pans?| transitions?)?\b|\bswipes?\b|\bslide transitions?\b", "whip"),
    (r"\bzoom(?:-| )?(?:transitions?|through|thru)\b|\bzoom between\b", "zoom"),
    (r"\b(?:dips? to white|white dips?|fade through white|white transitions?|flash transitions?)\b", "white"),
    (r"\bcut(?:s|ting)? (?:through|thru|via) black\b|\bblack (?:frames?|gaps?) between\b|\bblackout cuts?\b|"
     r"\bblack cuts\b|\bsnap(?:s|ping)? (?:to|through) black\b|\bblack between (?:the )?(?:cuts|shots|scenes)\b", "blackcut"),
    (r"\b(?:dips? to black|black dips?|fade through black|black transitions?)\b", "black"),
    (r"\bcircle (?:wipes?|transitions?|opens?)\b", "circle"),
    (r"\b(?:pixel(?:ate|ated|ize|ized)?|glitch|glitchy) transitions?\b|\bpixelat(?:e|ed|ion)\b", "pixel"),
    (r"\bwipes?\b|\bwipe transitions?\b", "wipe"),
]
DRUMS = [  # what "on the ..." can mean (pattern, drum)
    (r"snares?|claps?|backbeat", "snare"),
    (r"kicks?(?: drums?)?|bass drums?|808s?|bass hits?|the bass\b|bass", "kick"),
    (r"(?:big |hard |loud |strong )?hits?|accents|drum hits|drums", "hits"),
    (r"downbeats?|bars?|every bar", "bar"),
    (r"beats?", "beat"),
    (r"drops?", "drop"),
]
CONTENT = [  # (pattern, preferences, what to call it)
    (r"\b(?:in|into|through|across) (?:outer |deep )?space\b|\bouter space\b|\bspace(?= (?:battles?|fights?|fighting|scenes?|"
     r"shots?|combat|war|dogfights?)\b)|\bin orbit\b|\bamong the stars\b|\bstarfield\b|\basteroid fields?\b", {"bright": -0.6},
     "space (dark backgrounds)"),
    (r"action[- ]packed|punch(?:es|ing)?|fight(?:s|ing)?|kicking|attack(?:s|ing)?|combat|battle(?:s)?|beat ?downs?|combos?|"
     r"brawl(?:s|ing)?|clash(?:es|ing)?|sword ?fights?|gun ?fights?|shoot ?outs?|chases?|action(?: scenes| shots)?",
     {"motion": 1.0}, "action shots"),
    (r"explosions?|explod\w+|blasts?|beams?|energy (?:blasts?|attacks?)|kamehameha|final flash|galick gun|lasers?",
     {"motion": 0.6, "bright": 1.0}, "bright blasts and explosions"),
    (r"fire|flames?|burning|lava|sunset", {"bright": 0.7, "warm": 1.0}, "fiery, warm shots"),
    (r"auras?|power(?:ing)? ?ups?|powers up|transform(?:ation|ations|ing|s)?|super saiyan|charging|glowing", {"bright": 1.0, "sat": 0.6},
     "glowing power-up shots"),
    (r"lightning|electric(?:ity)?|thunder", {"bright": 0.8, "hue": 220.0}, "electric blue shots"),
    (r"yell(?:s|ing)?|scream(?:s|ing)?|shout(?:s|ing)?|roar(?:s|ing)?|rage|raging|angry|furious|battle cry",
     {"bright": 0.5, "sat": 0.6}, "intense shots (yelling, rage)"),
    (r"close[- ]?ups?|faces?|face shots|talking|dialogue|conversations?|staring|eyes|looking|reaction shots?",
     {"motion": -1.0}, "close-ups and faces"),
    (r"quiet scenes|calm scenes|emotional scenes|sad scenes|crying|tears|scenery|landscapes?|establishing shots|"
     r"the sky|clouds|peaceful scenes", {"motion": -1.0}, "calm scenes"),
    (r"bright(?:er)? (?:scenes|shots|parts|moments)|daylight|sunny", {"bright": 1.0}, "bright scenes"),
    (r"dark(?:er)? (?:scenes|shots|parts|moments)?|night(?:time)?|shadows?|moody|gloomy|grim|mysterious|eerie|creepy|spooky",
     {"bright": -1.0}, "dark scenes"),
    (r"no movement|not moving|still shots|static shots|calm shots|nothing moving", {"motion": -1.0}, "still, calm shots"),
    (r"colou?rful|vibrant|saturated|neon", {"sat": 1.0}, "colourful shots"),
    (r"red (?:scenes|shots|parts)?|bloody|blood", {"hue": 0.0}, "red shots"),
    (r"blue (?:scenes|shots|parts)?|ocean|water|underwater|ice|icy", {"hue": 220.0}, "blue shots"),
    (r"green (?:scenes|shots|parts)?|forest|grass|nature", {"hue": 120.0}, "green shots"),
    (r"purple (?:scenes|shots|parts)?|pink (?:scenes|shots|parts)?", {"hue": 290.0}, "purple shots"),
    (r"gold(?:en)? (?:scenes|shots|parts)?|yellow (?:scenes|shots|parts)?", {"hue": 50.0, "bright": 0.4}, "golden shots"),
]
QUERY_SKIP = set("""about above after again against also another around away back because been before being below
between both but can could did does doing down during each even ever every few first from further give going gonna got
had has have having her here hers him his how into its just last later least less let lets like made make many more
most much must near need next not now off once one only other our out over own part parts put really right same
see seem seems shall she should show some something song still such take than that the their them then there these
they thing things this those though through till too under until upon very want was way well were what when where
which while who whom why will with within without would yet you your yours start end ending middle beginning intro
outro verse chorus drop drops bridge build section sections second seconds minute minutes time times bit lot lots
character characters people person guy guys scene scenes shot shots clip clips footage video edit edits cut cuts
moment moments possible get gets getting look looks looking happen happens happening put putting use using try
final screen bunch frame frames image picture whole entire every each other way long short too much fast slow
focus focused focuses focusing one two three single just only beat beats bass include includes including packed
shows showing drawn real vibe vibes juxtaposition contrast subject matter liter literal literally kinda pretty fun
crazy insane wild chill hype hyped intense calm epic hard harder go goes going here there this that
dont doesnt wont cant isnt not never no any anything nothing lot lots alot thing stuff kind sure okay maybe
longer shorter play plays playing less fewer more
quick quicker slower faster cool sick nice good great best better bad kind sort feel
search find finding look looking scene battle play plays playing out through here""".split())
FILLER = re.compile(r"\b(i want|i'd like|i would like|i need|please|make it|make the|make this|the edit|this edit|edit|it|"
                    r"should|would|be|have|has|to|of|the|a|an|and|with|for|in|on|is|are|that|this|my|me|i|you|"
                    r"thanks|thank you|ok|okay|like|just|really|very|so|some|lot|lots|also|too|then|there|"
                    r"gonna|wanna|vibe|vibes|feel|feeling|kind|type|style|song|music|video|clip|clips|shots|"
                    r"scenes|parts|part|all|whole|time|way|look|looks|show|use|put|get|go|do|make|can|maybe|"
                    r"good|nice|cool|awesome|sick|dope|fire|lit|bro|dude|yeah|yes|right|well|keep|let|we|our|its|"
                    r"it's|i'm|im|want|wants|need|needs|cut|cuts|edits|editing)\b")


def _rx(p: str) -> re.Pattern:
    return re.compile(r"(?<![\w-])(?:" + p + r")(?![\w])")


EFFECT_RX = [(_rx(p), e) for p, e in EFFECT_WORDS]
FAST_RX, SLOW_RX, NORMAL_RX = _rx(FAST), _rx(SLOW), _rx(NORMAL)
HOLD_RX, RAMP_RX, VELOCITY_RX, PLAY_OUT_RX = _rx(HOLD), _rx(RAMP), _rx(VELOCITY), _rx(PLAY_OUT)
CONTENT_RX = [(_rx(p), pref, name) for p, pref, name in CONTENT]
DRUM_ALT = "|".join(f"(?:{p})" for p, _ in DRUMS)
SECTION_RX = _rx("|".join(sorted((re.escape(w) for w in SECTION_WORDS), key=len, reverse=True)))
ORDINAL_RX = re.compile(r"(?<![\w-])(first|1st|second|2nd|third|3rd|fourth|4th|last|final)\s+(?:big\s+|main\s+)?("
                        + "|".join(sorted((re.escape(w) for w in SECTION_WORDS), key=len, reverse=True)) + r")\b")
REST_RX = _rx(r"the rest|everything else|rest of (?:it|the (?:song|edit|video))|the whole (?:time|thing|song|edit|video)|"
              r"throughout|all the way through|the entire (?:song|edit|video)|overall|in general")


def _drum_of(word: str) -> str:
    for p, d in DRUMS:
        if re.fullmatch(p, word.strip()):
            return d
    return "hits"




GLOBAL_HINTS = [
    # (pattern, what it does, note)
    (r"\b(?:sync|synced|match|follow|flow with|move with|vibe with|cut to|cuts? on|feel)\b[\w\s]{0,25}\b(?:the )?"
     r"(?:music|song|beat|rhythm|energy|groove)\b|\bon beat\b|\bbeat ?sync(?:ed)?\b|"
     r"\b(?:slow|calm|quiet|chill) parts?\b.{0,30}\b(?:fast|hype|loud|intense|crazy) parts?\b", "follow", "following the music (always on)"),
    (r"\b(?:lots of|more|many|smooth|cool|sick) transitions\b|\btransitions? between\b", "transitions",
     "transitions: soft blurs into each part, white fades into the drops"),
    (r"\bjump cuts\b|\bhard cuts\b|\bno transitions\b|\bstraight cuts\b", "hard_cuts", "hard cuts, no transitions"),
    (r"\bepic\b|\blegendary\b|\bheroic\b|\bhype trailer\b", "epic", "epic: a freeze before the drop, white flash into it"),
    (r"\banime (?:opening|op|intro)\b|\bopening sequence\b|\b(?:op|ed) style\b", "opening",
     "anime opening feel: punchy cuts on the drum fills, a freeze before the drop"),
    (r"\bunder control\b|\bdon't overdo it\b|\bnot too much\b|\btasteful\b|\bnot over the top\b", "simple",
     "toned down: lighter effects"),
    (r"\b(?:dark|menacing|sinister|evil|horror|grim|villain(?:ous)?)\b[\w\s,]{0,20}\b(?:music video|mv|rap video|"
     r"edit|amv|vibe)\b|\bhellsing\b|\bberserk\b", "dark_mv",
     "dark music video: dark red grade, cuts through black, black flicker (heaviest in the intro), letterbox"),
    (r"\bphonk\b", "phonk", "phonk edit: heavy shake and zooms on the bass, cuts on the kick in the drop"),
    (r"\bamvs?\b|\bgmvs?\b|\b20(?:0\d|1[0-3])\b|\bold (?:school )?(?:youtube|amv|edit)s?\b|\bnostalgi\w+\b|\bclassic edit\b",
     "amv", "2000s AMV feel: glow, ghost trails, white flashes into the drop"),
    (r"\btrailer\b|\bmovie\b|\bfilm(?:ic)?\b", "trailer", "trailer feel: letterbox, film grade, a freeze before the drop"),
    (r"\b(?:keep it )?simple\b|\bminimal(?:ist)?\b|\bnothing (?:too )?fancy\b|\bbasic\b|\bsubtle\b|\bclean\b", "simple",
     "simple: lighter effects"),
    (r"\bsmooth(?:er)?(?: transitions)?\b|\bsoft transitions\b|\bfluid\b|\bseamless\b", "smooth",
     "smooth: soft blur transitions, gentler flashes"),
    (r"\bfreeze(?: frame)? (?:right )?(?:before|on|at|into) the drop\b", "freeze_drop", "freeze frame before the drop"),
    (r"\bwhite (?:flash|fade) (?:in)?to the drop\b|\bfade (?:in )?from white\b", "white_drop", "white fade into the drop"),
]
LOOKS = [
    (r"\bvhs\b|\bretro\b|\bgrain(?:y)?\b|\bcamcorder\b|\bold school\b", "vhs", "VHS look"),
    (r"\bblack (?:and|&|n) white\b|\bb&w\b|\bmonochrome\b|\bgr[ae]yscale\b|\bno colou?r\b", "mono", "black and white"),
    (r"\by2k\b|\b2000s\b|\b2010s\b|\bold youtube\b|\bnostalgi\w*\b", "y2k", "Y2K colour"),
    (r"\bteal\b|\borange and teal\b|\bmovie\b|\bfilm(?:ic)?\b|\bcinematic\b|\btrailer\b", "cinematic", "film grade"),
    (r"\bsad\b|\bmelanchol\w*\b|\bdepressing\b|\brain(?:y)?\b|\bgloomy\b", "sad", "sad grade"),
    (r"\bdream(?:y)?\b|\bethereal\b|\bheavenly\b", "dream", "dreamy grade"),
    (r"\b(?:dark|blood(?:y)?|deep)[- ]red (?:look|grade|tint|colou?rs?|tones?)\b|\bred (?:tint|grade|look)\b|\bhorror\b|"
     r"\bgritty\b|\bdark (?:look|grade|colou?rs?|tones?|aesthetic|colou?r grade)\b|\bcrushed blacks\b|\bmoody grade\b",
     "dark", "dark red grade"),
]
