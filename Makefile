# ATLAS - thin wrapper around start.sh (Linux/macOS). Windows: use start.bat / start.ps1.
.PHONY: help setup test status probe trade loop dashboard stop kill resume
help:      ; @echo "make setup | test | status | probe | trade | loop | dashboard | stop | kill REASON='...' | resume"
setup:     ; ./start.sh setup
test:      ; ./start.sh test
status:    ; ./start.sh status
probe:     ; ./start.sh probe
trade:     ; ./start.sh trade
loop:      ; ./start.sh loop
dashboard: ; ./start.sh dashboard
stop:      ; ./start.sh stop
kill:      ; ./start.sh kill "$(REASON)"
resume:    ; ./start.sh resume
