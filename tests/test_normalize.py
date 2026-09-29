from music_discovery.normalize import normalize, normalized_key


def test_normalize_strips_features_remasters_and_punctuation():
    assert normalize("Karma Police (Remastered)") == "karma police"
    assert normalize("Karma Police - 2011 Remaster") == "karma police"
    assert normalize("Karma Police (2015 Remaster)") == "karma police"
    assert normalize("Weird Fishes (feat. Nobody)") == "weird fishes"
    assert normalize("Weird Fishes [ft. Nobody]") == "weird fishes"
    assert normalize("AC/DC") == "ac dc"
    assert normalize("Björk") == "bjork"


def test_normalized_key_matches_a_remaster_of_the_same_song():
    heard = normalized_key("Radiohead", "Karma Police")
    remaster = normalized_key("Radiohead", "Karma Police (Remastered)")
    assert heard == remaster
