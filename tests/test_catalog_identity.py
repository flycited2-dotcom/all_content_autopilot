from orchestrator_bot.catalog_selection import resolve_identity


def test_brand_written_in_title_is_not_sent_empty_to_auditor():
    row = {'key': 'excel|термопот blackton tp433', 'brand': '', 'model': 'Термопот Blackton TP433', 'price': 100}
    fixed = resolve_identity(row, {'Blackton', 'BQ'})
    assert fixed == {**row, 'brand': 'Blackton', 'model': 'TP433'}
    assert row['brand'] == ''  # Source records and supplier keys stay intact.


def test_existing_brand_and_model_suffix_are_preserved():
    row = {'key': 'x', 'brand': 'BQ', 'model': 'TP5002-RU'}
    assert resolve_identity(row, {'BQ', 'Blackton'}) is row
    missing = {'key': 'y', 'brand': '', 'model': 'Термопот Blackton TP5002-RU'}
    assert resolve_identity(missing, {'Blackton'})['model'] == 'TP5002-RU'


def test_unknown_ambiguous_or_not_exact_brand_is_not_invented():
    for title in ('Термопот TP500', 'Blackton BQ TP500', 'НеBlackton TP500', 'Blackton 500', 'Blackton TP500 или TP433'):
        row = {'key': title, 'brand': '', 'model': title}
        assert resolve_identity(row, {'Blackton', 'BQ'}) is row
