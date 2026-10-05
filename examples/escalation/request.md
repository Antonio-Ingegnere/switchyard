Assess the supplied cache experiment without modifying files or running commands.
This workflow deliberately separates three bounded reasoning tasks:

Rung 0 is an initial hypothesis pass only. Record the provisional hypothesis
"the cache capacity is too small, causing the repeated backend calls" and your
observations in a structured handoff. Do not finalize the diagnosis; inconclusive
means that the hypothesis still requires the next stage's challenge.

Rung 1 continues from that handoff. Test its capacity hypothesis against the
supplied observations, explain why it is disproved, and record your findings in
a structured handoff. diagnosis_disproved hands assessment to an independent stage.

Rung 2 performs the final independent assessment from the durable evidence alone.
Explain the cause and a bounded correction in a structured handoff, returning ready.
Every rung must return a handoff. Treat all hypotheses as reasoning, not facts.
