/* =========================================================================
   RB-DAT Converter — frontend logic (vanilla JavaScript)
   Communicates with the Flask API. No frameworks, no fake progress.
   ========================================================================= */

(function () {
    "use strict";

    // Must match the backend MAX_FILE_SIZE exactly (2.00 GB in bytes).
    var MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024;

    // Status polling interval (ms).
    var POLL_INTERVAL = 1500;

    function $(id) { return document.getElementById(id); }

    var els = {
        dropZone:        $("dropZone"),
        fileInput:       $("fileInput"),
        chooseBtn:       $("chooseBtn"),
        fileInfo:        $("fileInfo"),
        fileName:        $("fileName"),
        fileSize:        $("fileSize"),
        fileValidation:  $("fileValidation"),
        uploadProgress:  $("uploadProgress"),
        uploadStatus:    $("uploadStatus"),
        uploadPercent:   $("uploadPercent"),
        uploadFill:      $("uploadFill"),
        convertProgress: $("convertProgress"),
        convertStatus:   $("convertStatus"),
        convertPercent:  $("convertPercent"),
        convertFill:     $("convertFill"),
        convertBtn:      $("convertBtn"),
        downloadBtn:     $("downloadBtn"),
        resetBtn:        $("resetBtn"),
        errorMessage:    $("errorMessage"),
        successMessage:  $("successMessage")
    };

    var currentFile = null;
    var currentJobId = null;
    var statusTimer = null;
    var isBusy = false;

    // ---------- helpers ----------

    function formatBytes(bytes) {
        if (!bytes || bytes < 0) return "0 B";
        var k = 1024;
        var sizes = ["B", "KB", "MB", "GB", "TB"];
        var i = Math.floor(Math.log(bytes) / Math.log(k));
        if (i >= sizes.length) i = sizes.length - 1;
        return (bytes / Math.pow(k, i)).toFixed(2) + " " + sizes[i];
    }

    function show(el)  { if (el) el.classList.remove("hidden"); }
    function hide(el)  { if (el) el.classList.add("hidden"); }

    function showError(msg) {
        if (els.errorMessage) {
            els.errorMessage.textContent = msg;
            show(els.errorMessage);
        }
        if (els.successMessage) hide(els.successMessage);
    }

    function showSuccess(msg) {
        if (els.successMessage) {
            els.successMessage.textContent = msg;
            show(els.successMessage);
        }
        if (els.errorMessage) hide(els.errorMessage);
    }

    function clearMessages() {
        hide(els.errorMessage);
        hide(els.successMessage);
    }

    function validateFile(file) {
        if (!file) return { valid: false, message: "No file selected." };
        var name = (file.name || "").toLowerCase();
        if (!name.endsWith(".dat")) {
            return { valid: false, message: "Only .dat files are accepted." };
        }
        if (file.size === 0) {
            return { valid: false, message: "File is empty." };
        }
        if (file.size > MAX_FILE_SIZE) {
            return {
                valid: false,
                message: "File is too large. Maximum size is 2.00 GB. " +
                         "Your file is " + formatBytes(file.size) + "."
            };
        }
        return { valid: true, message: "Valid .dat file ready to convert." };
    }

    function setFileInfo(file) {
        els.fileName.textContent = file.name;
        els.fileSize.textContent = formatBytes(file.size);
        var v = validateFile(file);
        els.fileValidation.textContent = v.message;
        els.fileValidation.className = "file-info-value " + (v.valid ? "valid" : "invalid");
        show(els.fileInfo);
    }

    function setUploadProgress(percent, statusText) {
        var p = Math.max(0, Math.min(100, percent));
        els.uploadFill.style.width = p + "%";
        els.uploadPercent.textContent = p + "%";
        if (statusText) els.uploadStatus.textContent = statusText;
    }

    function setConvertProgress(percent, statusText) {
        if (typeof percent === "number" && percent >= 0) {
            els.convertFill.classList.remove("indeterminate");
            els.convertFill.style.width = "";
            var p = Math.max(0, Math.min(100, percent));
            els.convertFill.style.width = p + "%";
            els.convertPercent.textContent = p + "%";
        } else {
            els.convertFill.classList.add("indeterminate");
            els.convertFill.style.width = "";
            els.convertPercent.textContent = "";
        }
        if (statusText) els.convertStatus.textContent = statusText;
    }

    function resetUI() {
        currentFile = null;
        currentJobId = null;
        isBusy = false;
        if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }

        if (els.fileInput) els.fileInput.value = "";

        hide(els.fileInfo);
        hide(els.uploadProgress);
        hide(els.convertProgress);
        hide(els.convertBtn);
        hide(els.downloadBtn);
        hide(els.resetBtn);
        clearMessages();

        els.uploadFill.style.width = "0%";
        els.uploadPercent.textContent = "0%";
        els.convertFill.style.width = "0%";
        els.convertFill.classList.remove("indeterminate");
        els.convertPercent.textContent = "";

        els.convertBtn.disabled = false;
        els.downloadBtn.onclick = null;
        els.dropZone.classList.remove("disabled");
    }

    function handleFile(file) {
        if (isBusy) return;
        resetUI();
        currentFile = file;
        setFileInfo(file);
        var v = validateFile(file);
        if (v.valid) {
            show(els.convertBtn);
            els.convertBtn.disabled = false;
        } else {
            showError(v.message);
        }
    }

    // ---------- drag & drop ----------

    ["dragenter", "dragover"].forEach(function (eventName) {
        els.dropZone.addEventListener(eventName, function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (!isBusy) els.dropZone.classList.add("drag-over");
        });
    });

    ["dragleave", "drop"].forEach(function (eventName) {
        els.dropZone.addEventListener(eventName, function (e) {
            e.preventDefault();
            e.stopPropagation();
            els.dropZone.classList.remove("drag-over");
        });
    });

    els.dropZone.addEventListener("drop", function (e) {
        if (isBusy) return;
        var files = e.dataTransfer && e.dataTransfer.files;
        if (files && files.length > 0) {
            handleFile(files[0]);
        }
    });

    // ---------- click to choose ----------

    els.chooseBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (isBusy) return;
        els.fileInput.click();
    });

    els.dropZone.addEventListener("click", function () {
        if (!isBusy) els.fileInput.click();
    });

    els.dropZone.addEventListener("keydown", function (e) {
        if (isBusy) return;
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            els.fileInput.click();
        }
    });

    els.fileInput.addEventListener("change", function (e) {
        if (e.target.files && e.target.files.length > 0) {
            handleFile(e.target.files[0]);
        }
    });

    // ---------- upload (XHR for progress) ----------

    function uploadFile(file) {
        return new Promise(function (resolve, reject) {
            var xhr = new XMLHttpRequest();
            var formData = new FormData();
            formData.append("file", file);

            xhr.open("POST", "/api/upload");

            xhr.upload.addEventListener("progress", function (e) {
                if (e.lengthComputable) {
                    var percent = Math.round((e.loaded / e.total) * 100);
                    setUploadProgress(percent,
                        percent < 100 ? "Uploading..." : "Upload complete. Preparing conversion...");
                }
            });

            xhr.addEventListener("load", function () {
                var data;
                try { data = JSON.parse(xhr.responseText); }
                catch (err) {
                    reject(new Error("Server returned an invalid response during upload."));
                    return;
                }
                if (xhr.status >= 200 && xhr.status < 300 && data.success) {
                    resolve(data);
                } else {
                    reject(new Error(data.error || ("Upload failed (HTTP " + xhr.status + ").")));
                }
            });

            xhr.addEventListener("error", function () {
                reject(new Error("Network error during upload. Please check your connection and try again."));
            });

            xhr.addEventListener("abort", function () {
                reject(new Error("Upload was cancelled."));
            });

            xhr.send(formData);
        });
    }

    function requestConversion(jobId) {
        return fetch("/api/convert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ job_id: jobId })
        }).then(function (res) {
            return res.json().then(function (data) {
                if (!res.ok || !data.success) {
                    throw new Error(data.error || "Failed to start conversion.");
                }
                return data;
            });
        });
    }

    // ---------- status polling ----------

    function pollStatus(jobId) {
        if (statusTimer) clearInterval(statusTimer);

        statusTimer = setInterval(function () {
            fetch("/api/status/" + jobId)
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (!data.success) {
                        throw new Error(data.error || "Failed to fetch job status.");
                    }
                    updateConversionUI(data);

                    if (data.status === "completed") {
                        clearInterval(statusTimer);
                        statusTimer = null;
                        onConversionComplete(jobId, data);
                    } else if (data.status === "failed") {
                        clearInterval(statusTimer);
                        statusTimer = null;
                        showError(data.error || "Conversion failed.");
                        show(els.resetBtn);
                        hide(els.convertProgress);
                        isBusy = false;
                        els.dropZone.classList.remove("disabled");
                    } else if (data.status === "expired" || data.status === "cleaned") {
                        clearInterval(statusTimer);
                        statusTimer = null;
                        showError("This job is no longer available. Please start again.");
                        show(els.resetBtn);
                        isBusy = false;
                        els.dropZone.classList.remove("disabled");
                    }
                })
                .catch(function (err) {
                    clearInterval(statusTimer);
                    statusTimer = null;
                    showError(err.message);
                    show(els.resetBtn);
                    isBusy = false;
                    els.dropZone.classList.remove("disabled");
                });
        }, POLL_INTERVAL);
    }

    function updateConversionUI(data) {
        if (data.status === "queued") {
            setConvertProgress(null, data.message || "Queued...");
            return;
        }
        if (data.status === "converting") {
            if (typeof data.progress === "number" && data.progress > 0) {
                setConvertProgress(data.progress, data.message || "Converting...");
            } else {
                setConvertProgress(null, data.message || "Converting...");
            }
            return;
        }
    }

    function onConversionComplete(jobId, data) {
        setConvertProgress(100, "Conversion complete.");
        var sizeText = data.output_size ? " (" + formatBytes(data.output_size) + ")" : "";
        showSuccess("Your MP4 file is ready for download." + sizeText);

        els.downloadBtn.onclick = function () {
            window.location.href = "/api/download/" + jobId;
        };
        show(els.downloadBtn);
        show(els.resetBtn);
        isBusy = false;
        els.dropZone.classList.remove("disabled");
    }

    // ---------- convert button ----------

    els.convertBtn.addEventListener("click", function () {
        if (!currentFile || isBusy) return;
        clearMessages();
        isBusy = true;
        els.convertBtn.disabled = true;
        hide(els.convertBtn);
        els.dropZone.classList.add("disabled");
        show(els.uploadProgress);
        setUploadProgress(0, "Uploading...");

        uploadFile(currentFile)
            .then(function (uploadRes) {
                currentJobId = uploadRes.job_id;
                setUploadProgress(100, "Upload complete.");
                show(els.convertProgress);
                setConvertProgress(null, "Starting conversion...");
                return requestConversion(currentJobId);
            })
            .then(function () {
                pollStatus(currentJobId);
            })
            .catch(function (err) {
                showError(err.message);
                show(els.resetBtn);
                hide(els.uploadProgress);
                hide(els.convertProgress);
                isBusy = false;
                els.dropZone.classList.remove("disabled");
            });
    });

    // ---------- reset button ----------

    els.resetBtn.addEventListener("click", function () {
        if (currentJobId) {
            // Best-effort cleanup; ignore errors.
            fetch("/api/cleanup/" + currentJobId, { method: "POST" })
                .catch(function () { /* ignored */ });
        }
        resetUI();
    });

})();
