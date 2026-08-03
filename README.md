# agentic-planner
 
A tool-execution planner where every argument has to prove where it came from.
 
The agent decides *which* tools to call and in what order; a separate
orchestrator runs them. That makes the deliverable a data structure rather than
an answer, and it means the plan can be checked before anything executes.
 
## The problem this is built around
 
A malformed plan announces itself. A well-formed plan containing one invented
number executes perfectly and returns a wrong answer, with nothing in the output
showing where the number came from.