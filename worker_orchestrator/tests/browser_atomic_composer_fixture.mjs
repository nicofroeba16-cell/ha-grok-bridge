#!/usr/bin/env node

import assert from 'node:assert/strict';

class EnterSubmittingComposer {
  constructor() {
    this.text = '';
    this.turns = [];
    this.mutations = 0;
  }

  legacyKeyboardType(payload) {
    for (const char of payload) {
      if (char === '\n') {
        this.turns.push(this.text);
        this.text = '';
      } else {
        this.text += char;
      }
      this.mutations += 1;
    }
  }

  atomicInsert(payload) {
    this.text = payload;
    this.mutations += 1;
  }

  sendOnce() {
    this.turns.push(this.text);
    this.text = '';
  }
}

function legacyDemonstratesPrefixRegression(payload) {
  const composer = new EnterSubmittingComposer();
  composer.legacyKeyboardType(payload);
  composer.sendOnce();
  return composer.turns;
}

function atomicDelivery(payload) {
  const composer = new EnterSubmittingComposer();
  composer.atomicInsert(payload);
  assert.equal(composer.turns.length, 0, 'atomic insertion must not submit');
  assert.equal(composer.text, payload, 'composer readback must equal full payload');
  composer.sendOnce();
  return { turns: composer.turns, mutations: composer.mutations };
}

function idleGate(states, stableSamples, onReady) {
  let stable = 0;
  for (const busy of states) {
    if (busy) {
      stable = 0;
      continue;
    }
    stable += 1;
    if (stable >= stableSamples) {
      onReady();
      return true;
    }
  }
  return false;
}

for (const payload of [
  'MASTER_WAKE\nWAKE_ID: master-1\nNEW_RELEVANT_EVENTS: 2\nACTION: coordinate',
  'WORKER_WAKE\nWAKE_ID: worker-1\nPROJECT: Auto Chat\nACTION: continue',
]) {
  const legacy = legacyDemonstratesPrefixRegression(payload);
  assert.equal(legacy[0], payload.split('\n')[0], 'legacy newline typing must reproduce prefix submit');
  assert.ok(legacy.length > 1, 'legacy path must demonstrate split-turn risk');

  const atomic = atomicDelivery(payload);
  assert.deepEqual(atomic.turns, [payload], 'atomic path must create exactly one full turn');
  assert.equal(atomic.mutations, 1, 'atomic path must use one composer mutation');
}

{
  const composer = new EnterSubmittingComposer();
  // Twelve 250 ms samples model a conversation generating for ~3 seconds.
  const becameIdle = idleGate(Array(12).fill(true), 6, () => {
    composer.atomicInsert('MASTER_WAKE\nWAKE_ID: must-not-send');
    composer.sendOnce();
  });
  assert.equal(becameIdle, false);
  assert.equal(composer.mutations, 0, 'busy timeout must cause zero composer mutation');
  assert.deepEqual(composer.turns, [], 'busy timeout must cause zero send');
}

{
  const payload = 'MASTER_WAKE\nWAKE_ID: after-idle';
  const composer = new EnterSubmittingComposer();
  // ~3 seconds busy followed by the same six-sample/~1.5 second stable idle gate.
  const becameIdle = idleGate([...Array(12).fill(true), ...Array(6).fill(false)], 6, () => {
    composer.atomicInsert(payload);
    composer.sendOnce();
  });
  assert.equal(becameIdle, true);
  assert.deepEqual(composer.turns, [payload], 'wake must arrive only after stable idle');
}

process.stdout.write(JSON.stringify({
  ok: true,
  cases: ['master-atomic', 'worker-atomic', 'busy-zero-mutation', 'stable-idle-delivery'],
}) + '\n');
