"""Koblinger mot tredjeparter. Hver modul har en demo-gren (ingen nettverk) og en prod-gren.

Regel: forretningslogikken (sveiper.py, daglig.py) kaller KUN funksjonene her – aldri API-ene direkte.
Da kan en integrasjon byttes (f.eks. Visma eAccounting -> Visma Business NXT) uten aa rore resten.
"""
