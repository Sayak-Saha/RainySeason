import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

# Load ONNX model and tokenizer once
tokenizer = AutoTokenizer.from_pretrained("assets/tokenizer", local_files_only=True)
session = ort.InferenceSession("assets/model.onnx", providers=["CPUExecutionProvider"])

def embed(text: str) -> np.ndarray:
    # Truncate aggressively for RAM
    inputs = tokenizer(
        text,
        return_tensors="np",
        padding="max_length",
        truncation=True,
        max_length=128,  # ✅ Reduce max_length to save RAM
        return_token_type_ids=True
    )

    # Fallback if token_type_ids not returned
    if "token_type_ids" not in inputs:
        inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])

    # Run ONNX inference
    ort_inputs = {
        "input_ids": inputs["input_ids"],
        "attention_mask": inputs["attention_mask"],
        "token_type_ids": inputs["token_type_ids"]
    }

    # Inference + pooling
    outputs = np.asarray(session.run(None, ort_inputs)[0])  # shape: [1, seq_len, hidden_size]
    pooled = np.mean(outputs, axis=1)[0]        # mean pool across seq_len
    return pooled.astype(np.float32)            # Return 1D float32 vector