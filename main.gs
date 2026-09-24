costumes "assets/hello.svg" as "hello";

var seconds = 0;
var keys = 0;
var armed = false;
var tx = 0;
var ty = 0;

onflag {
    seconds = 0;
    show;
    log "[hello] green flag: GoboScript hello world started";
    say "Hello, World!";
    forever {
        seconds += 1;
        log ("Hello, World! second=" & seconds);
        wait 1;
    }
}

# Live-edit + input hook for smoke.txt: `set main.tx/ty` moves the sprite with no
# rebuild, and a space press bumps `main.keys`. The 96x96 face is solid #4c97ff at
# its centre, so `expectpixel tx 0 #4c97ff` observes a live edit.
onflag {
    keys = 0;
    armed = false;
    forever {
        goto tx, ty;
        if key_pressed("space") {
            armed = true;
        }
        else {
            if armed {
                keys += 1;
                log ("[hello] space pressed count=" & keys);
                armed = false;
            }
        }
        wait 0.05;
    }
}
