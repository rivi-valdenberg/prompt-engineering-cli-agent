from cli_agent.validator import check_format, check_syntax, looks_wrong_shell


def test_clean_single_line_passes():
    assert check_format("ipconfig") == []


def test_code_fence_is_a_format_issue():
    issues = check_format("```bash\nipconfig\n```")
    assert "markdown_code_fence" in issues


def test_explanation_is_a_format_issue():
    issues = check_format("ipconfig\nThis command shows your IP address.")
    assert "multiline_output(2 lines)" in issues
    assert "prose_explanation" in issues


def test_hebrew_prose_is_a_format_issue():
    assert "natural_language_leaked(hebrew)" in check_format("ipconfig - זו הפקודה")


def test_quoted_hebrew_filename_is_not_a_format_issue():
    assert check_format('copy "דוח שנתי.docx" backup') == []


def test_shell_prompt_prefix_is_a_format_issue():
    assert "leading_shell_prompt" in check_format("$ ipconfig")


def test_control_replies_are_not_prose():
    assert check_format("BLOCKED: formatting a drive erases all data") == []
    assert check_format('CLARIFY: which folder should be cleaned?') == []


def test_empty_output():
    assert check_format("   ") == ["empty_output"]


def test_syntax_ok():
    assert check_syntax("dir /o-s", posix=False) == []
    assert check_syntax("ls -lhS", posix=True) == []


def test_unbalanced_quotes():
    assert "unbalanced_quotes" in check_syntax('copy "a.txt backup', posix=False)


def test_placeholder_is_a_syntax_issue():
    assert "unfilled_placeholder" in check_syntax("tail -n 20 <file>", posix=True)
    assert "unfilled_placeholder" in check_syntax("cat /path/to/file", posix=True)


def test_dangling_operator():
    assert "dangling_operator" in check_syntax("dir &&", posix=False)


def test_sentence_is_not_a_command():
    issues = check_syntax("please show me the running processes now", posix=True)
    assert "looks_like_a_sentence" in issues


def test_wrong_shell_detection():
    assert looks_wrong_shell("ls -lhS", "cmd") == "posix_utility_in_cmd"
    assert looks_wrong_shell("echo $PATH", "cmd") == "posix_variable_syntax_in_cmd"
    assert looks_wrong_shell("dir /o-s", "bash") == "windows_utility_in_bash"
    assert looks_wrong_shell("ipconfig", "cmd") is None
