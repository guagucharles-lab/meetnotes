

def test_old_default_model_moves_to_fable(tmp_path):
    from meetnotes import config
    p = tmp_path / "config.toml"
    p.write_text('claude_model = "claude-opus-5-5"\nassistant_model = "claude-sonnet-5-5"\n')
    cfg = config.load(p)
    assert cfg.claude_model == "claude-fable-5-2"          # the old default follows the new one
    assert cfg.assistant_model == "claude-sonnet-5-5"      # a deliberate choice is kept
    config.save(cfg, p)
    cfg.claude_model = "claude-opus-5-5"                   # choosing Opus again later sticks
    config.save(cfg, p)
    assert config.load(p).claude_model == "claude-opus-5-5"
