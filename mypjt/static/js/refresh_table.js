document.addEventListener("DOMContentLoaded", function () {
    const tableBody = document.getElementById("table-body");
    const tableContainer = document.getElementById("table-container");
    const tableName = window.location.pathname.split("/").pop(); // URLからテーブル名を取得

    function fetchTableData() {
        fetch(`/table/${tableName}`)
            .then(response => {
                if (!response.ok) {
                    throw new Error(`サーバーエラー: ${response.status}`);
                }
                return response.text(); // 📌 HTML を取得
            })
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, "text/html");
                const newTableBody = doc.getElementById("table-body");

                if (newTableBody) {
                    tableBody.innerHTML = newTableBody.innerHTML;
                    console.log("✅ テーブル更新完了！");
                }
            })
            .catch(error => console.error("❌ データの取得に失敗しました:", error));
    }

    fetchTableData(); // 初回実行
    setInterval(fetchTableData, TABLE_REFRESH_INTERVAL); // 5秒ごとに更新
});
