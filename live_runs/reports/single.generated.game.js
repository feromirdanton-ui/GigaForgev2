const BOARD_WIDTH = 10;
const BOARD_HEIGHT = 20;
let board = Array(BOARD_HEIGHT).fill().map(() => Array(BOARD_WIDTH).fill(null));
let currentPiece = null;
let nextPiece = null;
let heldPiece = null;
let canHold = true;
let score = 0;
let level = 1;
let linesCleared = 0;
let gameOver = false;
let paused = false;
let bag = [];
let pieces = [
    [[0, 0], [1, 0], [2, 0], [3, 0]], // I
    [[0, 0], [1, 0], [1, 1], [2, 1]], // J
    [[0, 1], [1, 1], [1, 0], [2, 0]], // L
    [[0, 0], [1, 0], [0, 1], [1, 1]], // O
    [[0, 1], [1, 1], [1, 0], [2, 0]], // S
    [[0, 0], [1, 0], [1, 1], [2, 1]], // T
    [[0, 0], [1, 0], [1, 1], [2, 1]]  // Z
];
let colors = ['cyan', 'blue', 'orange', 'yellow', 'green', 'purple', 'red'];

function createNewBag() {
    return [...Array(pieces.length).keys()].sort(() => Math.random() - 0.5);
}

function getRandomPiece() {
    if (!bag.length) {
        bag = createNewBag();
    }
    const idx = bag.pop();
    return { shape: pieces[idx], color: colors[idx] };
}

function renderBoard() {
    document.getElementById('game-board').innerHTML = '';
    for (let y = 0; y < BOARD_HEIGHT; y++) {
        let rowDiv = document.createElement('div');
        for (let x = 0; x < BOARD_WIDTH; x++) {
            let cellDiv = document.createElement('div');
            cellDiv.style.width = '20px';
            cellDiv.style.height = '20px';
            cellDiv.style.border = '1px solid gray';
            cellDiv.style.float = 'left';
            if (board[y][x]) {
                cellDiv.style.backgroundColor = board[y][x];
            } else {
                cellDiv.style.backgroundColor = 'black';
            }
            rowDiv.appendChild(cellDiv);
        }
        document.getElementById('game-board').appendChild(rowDiv);
    }
}

function renderPreview(previewType, piece) {
    const previewContainer = document.getElementById(`${previewType}-piece`);
    previewContainer.innerHTML = '';
    const size = 4;
    for (let i = 0; i < size * size; i++) {
        let div = document.createElement('div');
        div.style.width = `${size * 20}px`;
        div.style.height = `${size * 20}px`;
        div.style.border = '1px solid gray';
        div.style.display = 'inline-block';
        div.style.backgroundColor = piece ? piece.color : 'black';
        previewContainer.appendChild(div);
    }
}

function checkCollision(x, y, piece) {
    for (let i = 0; i < piece.shape.length; i++) {
        const px = x + piece.shape[i][0];
        const py = y + piece.shape[i][1];
        if (
            px >= BOARD_WIDTH ||
            px < 0 ||
            py >= BOARD_HEIGHT ||
            py < 0 ||
            board[py][px]
        ) {
            return true;
        }
    }
    return false;
}

function lockPieceInPlace() {
    for (let i = 0; i < currentPiece.shape.length; i++) {
        const px = currentPiece.x + currentPiece.shape[i][0];
        const py = currentPiece.y + currentPiece.shape[i][1];
        board[py][px] = currentPiece.color;
    }
}

function clearLines() {
    let rowsToRemove = [];
    for (let y = 0; y < BOARD_HEIGHT; y++) {
        let fullRow = true;
        for (let x = 0; x < BOARD_WIDTH; x++) {
            if (!board[y][x]) {
                fullRow = false;
                break;
            }
        }
        if (fullRow) {
            rowsToRemove.push(y);
        }
    }
    if (rowsToRemove.length > 0) {
        for (let r of rowsToRemove) {
            board.splice(r, 1);
            board.unshift(Array(BOARD_WIDTH).fill(null));
        }
        linesCleared += rowsToRemove.length;
        updateUI();
    }
}

function updateUI() {
    document.getElementById('score').textContent = `Score: ${score}`;
    document.getElementById('level').textContent = `Level: ${level}`;
    document.getElementById('lines').textContent = `Lines Cleared: ${linesCleared}`;
    renderBoard();
    renderPreview('next', nextPiece);
    renderPreview('hold', heldPiece);
}

function spawnPiece() {
    currentPiece = nextPiece || getRandomPiece();
    nextPiece = getRandomPiece();
    currentPiece.x = Math.floor((BOARD_WIDTH - currentPiece.shape.length) / 2);
    currentPiece.y = 0;
    if (checkCollision(currentPiece.x, currentPiece.y, currentPiece)) {
        gameOver = true;
        document.getElementById('status').textContent = 'Game Over!';
        return;
    }
    updateUI();
}

function movePiece(dx, dy) {
    if (checkCollision(currentPiece.x + dx, currentPiece.y + dy, currentPiece)) {
        return false;
    }
    currentPiece.x += dx;
    currentPiece.y += dy;
    return true;
}

function rotatePiece() {
    const rotatedShape = currentPiece.shape.map(
        ([x, y]) => [-y, x],
    );
    if (checkCollision(currentPiece.x, currentPiece.y, { ...currentPiece, shape: rotatedShape })) {
        return false;
    }
    currentPiece.shape = rotatedShape;
    return true;
}

function holdPiece() {
    if (!canHold) return;
    canHold = false;
    if (heldPiece === null) {
        heldPiece = currentPiece;
        spawnPiece();
    } else {
        [currentPiece, heldPiece] = [heldPiece, currentPiece];
        currentPiece.x = Math.floor((BOARD_WIDTH - currentPiece.shape.length) / 2);
        currentPiece.y = 0;
    }
    updateUI();
}

function tick() {
    if (gameOver || paused) return;
    if (!movePiece(0, 1)) {
        lockPieceInPlace();
        clearLines();
        spawnPiece();
    }
    updateUI();
    requestAnimationFrame(tick);
}

document.addEventListener('keydown', function(event) {
    switch (event.keyCode) {
        case 65: // Left arrow
            movePiece(-1, 0);
            break;
        case 68: // Right arrow
            movePiece(1, 0);
            break;
        case 87: // Up arrow
            rotatePiece();
            break;
        case 83: // Down arrow
            while(movePiece(0, 1)) {}
            break;
        case 32: // Space bar
            while(movePiece(0, 1)) {}
            break;
        case 80: // Pause key
            paused = !paused;
            if (paused) {
                document.getElementById('status').textContent = 'Paused';
            } else {
                document.getElementById('status').textContent = '';
            }
            break;
        default:
            break;
    }
});

document.getElementById('start-button').addEventListener('click', () => {
    gameOver = false;
    paused = false;
    board = Array(BOARD_HEIGHT).fill().map(() => Array(BOARD_WIDTH).fill(null));
    score = 0;
    level = 1;
    linesCleared = 0;
    bag = createNewBag();
    spawnPiece();
    tick();
});

document.getElementById('pause-button').addEventListener('click', () => {
    paused = !paused;
    if (paused) {
        document.getElementById('status').textContent = 'Paused';
    } else {
        document.getElementById('status').textContent = '';
    }
});

document.getElementById('restart-button').addEventListener('click', () => {
    gameOver = false;
    paused = false;
    board = Array(BOARD_HEIGHT).fill().map(() => Array(BOARD_WIDTH).fill(null));
    score = 0;
    level = 1;
    linesCleared = 0;
    bag = createNewBag();
    spawnPiece();
    tick();
});
