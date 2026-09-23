costumes "assets/hello.svg" as "hello";

var seconds = 0;

onflag {
    seconds = 0;
    log "[hello] green flag: GoboScript hello world started";
    say "Hello, World!";
    forever {
        seconds += 1;
        log ("Hello, World! second=" & seconds);
        wait 1;
    }
}
